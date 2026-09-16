"""Best-effort email summaries for governed identity automation.

Notification work deliberately runs only after the identity/QC transaction has
committed.  A mail queue failure is logged and returned to the caller; it never
changes the outcome of the governed work.
"""

from __future__ import annotations

import json
from functools import wraps
from html import escape
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import frappe
from frappe.utils import cint, get_url, now_datetime

from db_connector.notification_utils import parse_notification_recipients


SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
BATCH_DOCTYPE = "CCD Identity Activation Batch"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
MAX_DETAIL_RECORDS = 100


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
        return {"status": "Queued", "recipient_count": len(recipients)}
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
