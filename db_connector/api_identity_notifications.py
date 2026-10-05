"""Best-effort email summaries for governed identity automation.

Notification work deliberately runs only after the identity/QC transaction has
committed.  A mail queue failure is logged and returned to the caller; it never
changes the outcome of the governed work.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import wraps
from html import escape
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import frappe
from frappe.utils import cint, get_datetime, get_url, now_datetime

from db_connector.notification_utils import (
    is_orphaned_scheduler_job,
    parse_notification_recipients,
    should_hold_email_backlog,
)


SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
BATCH_DOCTYPE = "CCD Identity Activation Batch"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
MAX_DETAIL_RECORDS = 100
EMAIL_FLUSH_METHOD = "frappe.email.queue.flush"
EMAIL_FLUSH_JOB_ID = f"scheduled_job::{EMAIL_FLUSH_METHOD}"
EMAIL_FLUSH_ORPHAN_MINIMUM_AGE_SECONDS = 300
EMAIL_FLUSH_HEALTH_LOCK = "db_connector:email-flush-health"
EMAIL_FLUSH_AUTOMATIC_BACKLOG_LIMIT = 10
EMAIL_FLUSH_AUTOMATIC_BACKLOG_MAX_AGE_SECONDS = 24 * 60 * 60
EMAIL_FLUSH_BACKLOG_HOLD_DEFAULT = "db_connector_email_backlog_hold"


def _rq_job_age_seconds(job: Any) -> float:
    timestamp = job.started_at or job.enqueued_at or job.created_at
    if not timestamp:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - timestamp).total_seconds())


def _rq_job_is_registered(job: Any, connection: Any) -> bool:
    """Fail closed unless the RQ job is absent from every live location."""
    from rq import Queue, Worker
    from rq.registry import StartedJobRegistry

    origin = str(job.origin or "")
    if not origin:
        return True
    queue = Queue(origin, connection=connection)
    if job.id in set(queue.get_job_ids()):
        return True
    started = StartedJobRegistry(queue=queue)
    if job.id in set(started.get_job_ids()):
        return True
    for worker in Worker.all(queue=queue):
        current_job = worker.get_current_job()
        if current_job and current_job.id == job.id:
            return True
    return False


def _pending_email_count() -> int:
    return int(
        frappe.db.count(
            "Email Queue", filters={"status": ["in", ["Not Sent", "Partially Sent"]]}
        )
        or 0
    )


def _pending_email_state() -> dict[str, Any]:
    row = frappe.db.sql(
        """
        SELECT COUNT(*) AS pending_count, MIN(creation) AS oldest_creation
        FROM `tabEmail Queue`
        WHERE status IN ('Not Sent', 'Partially Sent')
        """,
        as_dict=True,
    )[0]
    count = int(row.pending_count or 0)
    oldest_age_seconds = 0.0
    if count and row.oldest_creation:
        oldest_age_seconds = max(
            0.0,
            (now_datetime() - get_datetime(row.oldest_creation)).total_seconds(),
        )
    return {
        "pending": count,
        "oldest_age_seconds": oldest_age_seconds,
    }


def _enqueue_email_flush() -> bool:
    job_type_name = frappe.db.get_value(
        "Scheduled Job Type", {"method": EMAIL_FLUSH_METHOD}, "name"
    )
    if not job_type_name:
        raise RuntimeError("Email flush Scheduled Job Type is missing")
    return bool(frappe.get_doc("Scheduled Job Type", job_type_name).enqueue(force=True))


def _set_automatic_backlog_hold(hold: bool) -> bool:
    """Own and release the global mail suspension only when this guard set it."""
    owned = bool(cint(frappe.db.get_default(EMAIL_FLUSH_BACKLOG_HOLD_DEFAULT)))
    suspended = bool(cint(frappe.db.get_default("suspend_email_queue")))
    changed = False
    if hold and not suspended:
        frappe.db.set_default("suspend_email_queue", 1)
        frappe.db.set_default(EMAIL_FLUSH_BACKLOG_HOLD_DEFAULT, 1)
        owned = True
        changed = True
    elif not hold and owned:
        frappe.db.set_default("suspend_email_queue", 0)
        frappe.db.set_default(EMAIL_FLUSH_BACKLOG_HOLD_DEFAULT, 0)
        owned = False
        changed = True
    if changed:
        frappe.db.commit()
    return owned


def ensure_email_flush_scheduler_health() -> dict[str, Any]:
    """Repair only a proven orphaned email-flush marker and queue pending mail."""
    from frappe.utils.background_jobs import get_job, get_redis_conn

    connection = get_redis_conn()
    lock = connection.lock(
        EMAIL_FLUSH_HEALTH_LOCK,
        timeout=30,
        blocking_timeout=1,
    )
    if not lock.acquire(blocking=True):
        return {"status": "Busy", "pending": _pending_email_count()}

    repaired = False
    try:
        pending_state = _pending_email_state()
        pending = pending_state["pending"]
        if should_hold_email_backlog(
            pending,
            pending_state["oldest_age_seconds"],
            maximum_count=EMAIL_FLUSH_AUTOMATIC_BACKLOG_LIMIT,
            maximum_age_seconds=EMAIL_FLUSH_AUTOMATIC_BACKLOG_MAX_AGE_SECONDS,
        ):
            hold_owned = _set_automatic_backlog_hold(True)
            frappe.logger("identity_notifications").error(
                "Holding email backlog before scheduler repair "
                "(pending=%s oldest_age_seconds=%s)",
                pending,
                int(pending_state["oldest_age_seconds"]),
            )
            return {
                "status": "Backlog Held",
                "pending": pending,
                "oldest_age_seconds": int(pending_state["oldest_age_seconds"]),
                "queue_suspended_by_guard": hold_owned,
                "enqueued": False,
            }
        _set_automatic_backlog_hold(False)

        job = get_job(EMAIL_FLUSH_JOB_ID)
        if job:
            status = job.get_status()
            registered = _rq_job_is_registered(job, connection)
            age_seconds = _rq_job_age_seconds(job)
            if is_orphaned_scheduler_job(
                status,
                registered_in_queue=registered,
                age_seconds=age_seconds,
                minimum_age_seconds=EMAIL_FLUSH_ORPHAN_MINIMUM_AGE_SECONDS,
            ):
                job.delete()
                repaired = True
                frappe.logger("identity_notifications").warning(
                    "Removed orphaned RQ marker %s (status=%s age_seconds=%s)",
                    job.id,
                    status,
                    int(age_seconds),
                )
            elif str(getattr(status, "value", status) or "").casefold() in {
                "queued",
                "started",
            }:
                return {
                    "status": "Healthy" if registered else "Grace Period",
                    "pending": pending,
                    "job_status": str(getattr(status, "value", status)),
                }

        enqueued = _enqueue_email_flush() if pending else False
        return {
            "status": "Repaired" if repaired else ("Enqueued" if enqueued else "Idle"),
            "pending": pending,
            "enqueued": enqueued,
        }
    finally:
        lock.release()


def run_email_flush_health_check() -> dict[str, Any]:
    """Scheduled fail-safe; errors are logged without affecting identity work."""
    try:
        return ensure_email_flush_scheduler_health()
    except Exception as exc:
        frappe.log_error(
            title="Email flush scheduler health check failed",
            message=frappe.get_traceback(),
        )
        return {
            "status": "Failed",
            "error": f"{type(exc).__name__}:{str(exc)[:180]}",
        }


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _doctype_route(doctype: str) -> str:
    return doctype.strip().casefold().replace(" ", "-")


def _desk_url(doctype: str, name: str = "") -> str:
    path = f"/app/{_doctype_route(doctype)}"
    if name and doctype != SETTINGS_DOCTYPE:
        path += f"/{quote(str(name), safe='')}"
    return get_url(path)


def _link(doctype: str, name: str, label: str = "") -> str:
    if not name:
        return ""
    text = label or f"{doctype} {name}"
    return (
        f'<a href="{escape(_desk_url(doctype, name), quote=True)}">'
        f"{escape(str(text))}</a>"
    )


def _value(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, default=str)
    return str(value)


def _summary_table(rows: Iterable[tuple[str, Any]]) -> str:
    rendered = "".join(
        "<tr>"
        f'<th align="left" style="padding:4px 12px 4px 0">{escape(label)}</th>'
        f'<td style="padding:4px 0">{escape(_value(value))}</td>'
        "</tr>"
        for label, value in rows
    )
    return f'<table style="border-collapse:collapse">{rendered}</table>'


def _notification_configuration() -> tuple[bool, tuple[str, ...]]:
    if not frappe.db.table_exists("Singles"):
        return False, ()
    enabled = bool(
        cint(
            frappe.db.get_single_value(
                SETTINGS_DOCTYPE, "automation_notifications_enabled"
            )
        )
    )
    recipients = parse_notification_recipients(
        frappe.db.get_single_value(
            SETTINGS_DOCTYPE, "automation_notification_recipients"
        )
    )
    return enabled, recipients


def _queue_email(subject: str, body: str) -> dict[str, Any]:
    enabled, recipients = _notification_configuration()
    if not enabled:
        return {"status": "Disabled", "recipient_count": 0}
    if not recipients:
        return {"status": "No Recipients", "recipient_count": 0}
    try:
        frappe.sendmail(
            recipients=list(recipients),
            subject=subject,
            message=body,
            delayed=True,
            reference_doctype=SETTINGS_DOCTYPE,
            reference_name=SETTINGS_DOCTYPE,
        )
        frappe.db.commit()
    except Exception as exc:
        # All callers invoke notification only after committing governed work.
        # Rolling back here can therefore affect only this failed queue attempt.
        frappe.db.rollback()
        frappe.log_error(
            title="Identity automation notification failed",
            message=frappe.get_traceback(),
        )
        return {
            "status": "Failed",
            "recipient_count": len(recipients),
            "error": f"{type(exc).__name__}:{str(exc)[:180]}",
        }
    health = run_email_flush_health_check()
    return {
        "status": "Queued",
        "recipient_count": len(recipients),
        "email_flush_health": health,
    }


def _best_effort_notification(function):
    """Keep rendering/query errors outside the governed transaction path."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            frappe.db.rollback()
            frappe.log_error(
                title="Identity automation notification rendering failed",
                message=frappe.get_traceback(),
            )
            return {
                "status": "Failed",
                "recipient_count": 0,
                "error": f"{type(exc).__name__}:{str(exc)[:180]}",
            }

    return wrapped


def _recommendation_links(names: Iterable[str]) -> list[str]:
    links = []
    for name in list(dict.fromkeys(str(item) for item in names if item))[
        :MAX_DETAIL_RECORDS
    ]:
        links.append(_link(RECOMMENDATION_DOCTYPE, name, name))
    return links


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if item] if isinstance(parsed, list) else []


def _batch_detail(batch_name: str) -> str:
    if not batch_name or not frappe.db.exists(BATCH_DOCTYPE, batch_name):
        return ""
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    parts = [
        "<h3>Automatic activation batch</h3>",
        f"<p>{_link(BATCH_DOCTYPE, batch.name, f'Open batch {batch.name}')}</p>",
        _summary_table(
            (
                ("Batch status", batch.status),
                ("Selected components", batch.selected_component_count),
                ("Selected recommendations", batch.selected_recommendation_count),
                ("Created groups", batch.created_group_count),
                ("Created memberships", batch.created_membership_count),
                ("New safety exceptions", batch.new_exception_count),
                ("Error summary", batch.error_summary),
            )
        ),
    ]
    item_rows = []
    for item in list(batch.items)[:MAX_DETAIL_RECORDS]:
        record_links = _recommendation_links(_json_list(item.recommendation_names_json))
        if item.identity_decision:
            record_links.append(
                _link("CCD Identity Decision", item.identity_decision, item.identity_decision)
            )
        if item.identity_group:
            record_links.append(
                _link("CCD Identity Group", item.identity_group, item.identity_group)
            )
        item_rows.append(
            "<tr>"
            f"<td>{escape(str(item.component_fingerprint or ''))}</td>"
            f"<td>{escape(str(item.status or ''))}</td>"
            f"<td>{escape(str(item.error_code or ''))}</td>"
            f"<td>{'<br>'.join(link for link in record_links if link) or '—'}</td>"
            "</tr>"
        )
    if item_rows:
        parts.extend(
            (
                "<h3>Finished component results</h3>",
                '<table border="1" cellpadding="5" cellspacing="0">'
                "<tr><th>Component fingerprint</th><th>Status</th>"
                "<th>Error / exception</th><th>Records</th></tr>"
                + "".join(item_rows)
                + "</table>",
            )
        )
    return "".join(parts)


def _skipped_component_detail(result: Mapping[str, Any]) -> str:
    rows = []
    for component in list(result.get("skipped_components") or [])[
        :MAX_DETAIL_RECORDS
    ]:
        names = component.get("recommendations") or component.get(
            "recommendation_names"
        ) or []
        links = _recommendation_links(names)
        for recommendation in frappe.get_all(
            RECOMMENDATION_DOCTYPE,
            filters={"name": ["in", list(names)]},
            fields=["name", "component_review"],
            limit_page_length=max(len(names), 1),
        ) if names else []:
            if recommendation.component_review:
                links.append(
                    _link(
                        "CCD Match Component Review",
                        recommendation.component_review,
                        f"exception {recommendation.component_review}",
                    )
                )
        rows.append(
            "<tr>"
            f"<td>{escape(str(component.get('component_fingerprint') or ''))}</td>"
            f"<td>{escape(_value(component.get('conflicts') or component.get('safety_reasons')))}</td>"
            f"<td>{'<br>'.join(link for link in links if link) or '—'}</td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        "<h3>Skipped unsafe / exception components</h3>"
        '<table border="1" cellpadding="5" cellspacing="0">'
        "<tr><th>Component fingerprint</th><th>Reason</th><th>Records</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _qc_monitor_detail(monitor_results: Iterable[Mapping[str, Any]]) -> str:
    rows = []
    investigations = []
    for result in monitor_results:
        run_name = str(result.get("run") or "")
        run_link = _link(RUN_DOCTYPE, run_name, run_name) if run_name else "—"
        rows.append(
            "<tr>"
            f"<td>{run_link}</td>"
            f"<td>{escape(_value(result.get('window_finalized')))}</td>"
            f"<td>{escape(_value(result.get('same')))}</td>"
            f"<td>{escape(_value(result.get('different')))}</td>"
            f"<td>{escape(_value(result.get('overdue')))}</td>"
            f"<td>{escape(_value(result.get('stale')))}</td>"
            f"<td>{escape(_value(result.get('automation_state')))}</td>"
            "</tr>"
        )
        investigations.extend(
            failure for failure in result.get("new_failures") or [] if failure
        )
    parts = []
    if rows:
        parts.append(
            "<h3>QC monitor results</h3>"
            '<table border="1" cellpadding="5" cellspacing="0">'
            "<tr><th>Canary</th><th>Rolling finalized</th><th>Same</th>"
            "<th>Different</th><th>Overdue</th><th>Stale</th><th>State</th></tr>"
            + "".join(rows)
            + "</table>"
        )
    if investigations:
        links = []
        for failure in investigations[:MAX_DETAIL_RECORDS]:
            if failure.get("investigation"):
                links.append(
                    _link(
                        INVESTIGATION_DOCTYPE,
                        str(failure["investigation"]),
                        f"investigation {failure['investigation']}",
                    )
                )
            if failure.get("recommendation"):
                links.extend(_recommendation_links([failure["recommendation"]]))
            if failure.get("current_shared_group"):
                links.append(
                    _link(
                        "CCD Identity Group",
                        str(failure["current_shared_group"]),
                        f"group {failure['current_shared_group']}",
                    )
                )
        parts.append("<h3>New QC failures / investigations</h3><p>" + "<br>".join(links) + "</p>")
    return "".join(parts)


def _header(title: str, introduction: str) -> str:
    return (
        f"<h2>{escape(title)}</h2>"
        f"<p>{escape(introduction)}</p>"
        f"<p><b>Completed at:</b> {escape(str(now_datetime()))}<br>"
        f"<b>Settings:</b> {_link(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE, 'Open CCD Identity Resolution Settings')}</p>"
    )


@_best_effort_notification
def notify_daily_monitor(result: Mapping[str, Any]) -> dict[str, Any]:
    automation = result.get("automatic_tiered") or {}
    cadence = result.get("qc_cadence") or {}
    status = str(
        result.get("status")
        if result.get("status") == "Failed"
        else automation.get("status") or result.get("status") or "Completed"
    )
    body = _header(
        "Daily identity automation monitor",
        "The scheduled QC monitor and its following bounded Tiered cycle have finished.",
    )
    body += _summary_table(
        (
            ("Daily monitor status", result.get("status")),
            ("QC cadence status", cadence.get("status")),
            ("New QC cases assigned", cadence.get("assigned", 0)),
            ("QC due at", cadence.get("due_at") or cadence.get("next_assignment_at")),
            ("Automatic Tiered status", automation.get("status")),
            ("Batch", automation.get("batch")),
            ("Created groups", automation.get("created_groups", 0)),
            ("Created memberships", automation.get("created_memberships", 0)),
            (
                "Skipped unsafe components",
                automation.get("skipped_unsafe_component_count", 0),
            ),
            (
                "Blockers / error",
                result.get("error")
                or automation.get("blockers")
                or automation.get("error"),
            ),
        )
    )
    assigned = cadence.get("recommendations") or []
    if assigned:
        body += "<h3>Newly assigned QC cases</h3><p>" + "<br>".join(
            _recommendation_links(assigned)
        ) + "</p>"
    body += _qc_monitor_detail(result.get("qc_monitors") or [])
    body += _batch_detail(str(automation.get("batch") or ""))
    body += _skipped_component_detail(automation)
    return _queue_email(f"[ERPNext Identity] Daily monitor: {status}", body)


@_best_effort_notification
def notify_manual_automatic_cycle(
    result: Mapping[str, Any], *, error: str = ""
) -> dict[str, Any]:
    status = str(result.get("status") or ("Failed" if error else "Completed"))
    body = _header(
        "Manual automatic Tiered cycle",
        "Run One Automatic Cycle Now has finished. This action runs the bounded Tiered cycle only; it does not run the daily QC monitor or QC assignment cadence.",
    )
    body += _summary_table(
        (
            ("Status", status),
            ("Batch", result.get("batch")),
            ("Created groups", result.get("created_groups", 0)),
            ("Created memberships", result.get("created_memberships", 0)),
            (
                "Canary active recommendations after cycle",
                result.get("approved_recommendations", 0),
            ),
            (
                "Skipped unsafe components",
                result.get("skipped_unsafe_component_count", 0),
            ),
            ("Blockers / error", error or result.get("blockers") or result.get("error")),
        )
    )
    body += _batch_detail(str(result.get("batch") or ""))
    body += _skipped_component_detail(result)
    return _queue_email(f"[ERPNext Identity] Manual automatic cycle: {status}", body)


@_best_effort_notification
def notify_qc_assignment(result: Mapping[str, Any]) -> dict[str, Any]:
    assigned = int(result.get("assigned") or 0)
    if not assigned:
        return {"status": "No New Cases", "recipient_count": 0}
    run_name = str(result.get("run") or "")
    body = _header(
        "New identity QC cases assigned",
        "A manager assignment released new QC work. The assigned recommendations are linked below.",
    )
    body += _summary_table(
        (
            ("Canary", run_name),
            ("Assigned", assigned),
            ("Replenished", result.get("replenished", 0)),
            ("Due at", result.get("due_at")),
        )
    )
    if run_name:
        body += f"<p>{_link(RUN_DOCTYPE, run_name, f'Open canary {run_name}')}</p>"
    body += "<h3>Assigned recommendations</h3><p>" + "<br>".join(
        _recommendation_links(result.get("recommendations") or [])
    ) + "</p>"
    return _queue_email(f"[ERPNext Identity] {assigned} new QC case(s) assigned", body)


@frappe.whitelist()
def send_test_automation_notification() -> dict[str, Any]:
    _require_manager()
    body = _header(
        "Identity automation notification test",
        "This test confirms that the configured recipients can be queued through ERPNext. It did not run QC monitoring or identity materialization.",
    )
    body += _summary_table(
        (
            ("Result", "Notification queue test"),
            ("Identity/QC records changed", "No"),
        )
    )
    return _queue_email("[ERPNext Identity] Notification test", body)
