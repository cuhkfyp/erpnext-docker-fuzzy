"""Continuous QC cadence, rolling precision, and governed circuit breakers."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

import frappe
from frappe.utils import add_days, get_datetime, now_datetime

from db_connector.api_identity_resolution import (
    CURRENT_MEMBERSHIP_STATUSES,
    _append_event,
    _current_memberships,
)
from db_connector.fuzzy_matching.automation import (
    cadence_due,
    current_shared_group,
    deterministic_qc_selection,
    rolling_qc_summary,
)

RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
MEMBERSHIP_DOCTYPE = "CCD Identity Membership"
GROUP_DOCTYPE = "CCD Identity Group"
FINAL_STATUSES = {"Agreed", "Adjudicated"}
OPEN_REVIEW_STATUSES = {
    "Unreviewed",
    "Partially Reviewed",
    "Positive Confirmation Required",
    "Needs Adjudication",
}
MAX_QC_ASSIGNMENT = 100
PRECISION_LOWER_TARGET = 0.95


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _lock_settings() -> None:
    frappe.db.sql(
        "SELECT field FROM `tabSingles` WHERE doctype = %s ORDER BY field FOR UPDATE",
        (SETTINGS_DOCTYPE,),
    )


def _lock_named_rows(doctype: str, names: Iterable[str]) -> None:
    ordered = tuple(sorted({str(name) for name in names if str(name)}))
    if not ordered:
        return
    placeholders = ", ".join(["%s"] * len(ordered))
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name IN ({placeholders}) "
        "ORDER BY name FOR UPDATE",
        ordered,
    )


def _set_single_values(values: dict[str, Any]) -> None:
    for fieldname, value in values.items():
        frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)


def _control_nonce(action: str, revision: int, reason: str) -> str:
    return hashlib.sha256(
        f"{action}\x1f{revision}\x1f{reason}\x1f{frappe.session.user}".encode()
    ).hexdigest()


def _append_control_event(
    *, event_type: str, action: str, revision: int, reason: str, metadata: dict[str, Any]
) -> str:
    return _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type=event_type,
        reason=reason,
        nonce=_control_nonce(action, revision, reason),
        from_status=str(metadata.get("from_status") or ""),
        to_status=str(metadata.get("to_status") or ""),
        metadata={"control": action, "revision": revision, **metadata},
    )


def _pause_automation(
    scope: str, reason: str, *, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Set the global Tiered breaker and append an immutable Pause event."""
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.automation_paused:
        # A circuit breaker is a state transition, not a daily heartbeat.
        # Later failures retain their own Investigation/QC Failure audit while
        # the original breaker cause remains visible until governed recovery.
        return {
            "revision": int(settings.automation_control_revision or 0),
            "event": "",
            "scope": str(settings.pause_scope or scope or "global"),
            "already_paused": True,
        }
    prior_state = "Paused" if settings.automation_paused else "Monitoring"
    revision = int(settings.automation_control_revision or 0) + 1
    _set_single_values(
        {
            "automation_paused": 1,
            "pause_scope": str(scope or "global"),
            "pause_reason": str(reason),
            "automation_control_revision": revision,
        }
    )
    event = _append_control_event(
        event_type="Pause",
        action="tiered_circuit_breaker",
        revision=revision,
        reason=str(reason),
        metadata={
            "from_status": prior_state,
            "to_status": "Paused",
            "scope": str(scope or "global"),
            **(metadata or {}),
        },
    )
    return {"revision": revision, "event": event, "scope": str(scope or "global")}


def _current_shared_group(recommendation: Any) -> str | None:
    memberships = _current_memberships(
        (str(recommendation.left_record), str(recommendation.right_record))
    )
    try:
        return current_shared_group(
            (dict(row) for row in memberships),
            str(recommendation.left_record),
            str(recommendation.right_record),
        )
    except ValueError as exc:
        frappe.throw(str(exc))


def _open_investigation(
    recommendation: Any, scope: str, reason: str, identity_group: str | None
) -> str:
    key = hashlib.sha256(f"{recommendation.name}\x1fqc-failure".encode()).hexdigest()
    existing = frappe.db.get_value(
        INVESTIGATION_DOCTYPE, {"investigation_key": key}, "name"
    )
    if existing:
        return str(existing)
    doc = frappe.get_doc(
        {
            "doctype": INVESTIGATION_DOCTYPE,
            "investigation_key": key,
            "canary_run": recommendation.canary_run,
            "recommendation": recommendation.name,
            "identity_decision": recommendation.identity_decision or None,
            "identity_group": identity_group or None,
            "pause_scope": scope,
            "status": "Open",
            "reason": reason,
            "opened_at": now_datetime(),
            "opened_by": frappe.session.user or "Administrator",
        }
    ).insert(ignore_permissions=True)
    return doc.name


def _suspend_group_for_qc(identity_group: str | None, investigation: str) -> int:
    if not identity_group:
        return 0
    _lock_named_rows(GROUP_DOCTYPE, (identity_group,))
    old_group_status = str(
        frappe.db.get_value(GROUP_DOCTYPE, identity_group, "status") or ""
    )
    memberships = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "identity_group": identity_group,
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=["name", "status", "originating_decision"],
        limit_page_length=100_000,
    )
    _lock_named_rows(MEMBERSHIP_DOCTYPE, (row.name for row in memberships))
    for membership in memberships:
        old_status = str(membership.status)
        frappe.db.set_value(
            MEMBERSHIP_DOCTYPE,
            membership.name,
            "status",
            "Needs Revalidation",
            update_modified=False,
        )
        _append_event(
            entity_doctype=MEMBERSHIP_DOCTYPE,
            entity_name=membership.name,
            event_type="QC Failure",
            reason="confirmed_qc_different",
            nonce=investigation,
            from_status=old_status,
            to_status="Needs Revalidation",
            identity_decision=membership.originating_decision,
            identity_group=identity_group,
            identity_membership=membership.name,
            metadata={"investigation": investigation},
        )
    frappe.db.set_value(
        GROUP_DOCTYPE,
        identity_group,
        "status",
        "Needs Revalidation",
        update_modified=False,
    )
    _append_event(
        entity_doctype=GROUP_DOCTYPE,
        entity_name=identity_group,
        event_type="QC Failure",
        reason="confirmed_qc_different",
        nonce=investigation,
        from_status=old_group_status,
        to_status="Needs Revalidation",
        identity_group=identity_group,
        metadata={"investigation": investigation},
    )
    return len(memberships)


def _revalidate_investigation_group(identity_group: str, investigation: str) -> int:
    """Restore one explicitly revalidated current group with immutable events."""
    _lock_named_rows(GROUP_DOCTYPE, (identity_group,))
    group_status = frappe.db.get_value(GROUP_DOCTYPE, identity_group, "status")
    if group_status != "Needs Revalidation":
        return 0
    memberships = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "identity_group": identity_group,
            "status": "Needs Revalidation",
        },
        fields=["name", "originating_decision"],
        limit_page_length=100_000,
    )
    _lock_named_rows(MEMBERSHIP_DOCTYPE, (row.name for row in memberships))
    if not memberships:
        frappe.throw(
            "The affected group has no current memberships to revalidate; "
            "use the complete-component correction workflow"
        )
    for membership in memberships:
        frappe.db.set_value(
            MEMBERSHIP_DOCTYPE,
            membership.name,
            "status",
            "Active",
            update_modified=False,
        )
        _append_event(
            entity_doctype=MEMBERSHIP_DOCTYPE,
            entity_name=membership.name,
            event_type="Revalidate",
            reason="governed_qc_investigation_resolution",
            nonce=investigation,
            from_status="Needs Revalidation",
            to_status="Active",
            identity_decision=membership.originating_decision,
            identity_group=identity_group,
            identity_membership=membership.name,
            metadata={"investigation": investigation},
        )
    frappe.db.set_value(
        GROUP_DOCTYPE,
        identity_group,
        "status",
        "Active",
        update_modified=False,
    )
    _append_event(
        entity_doctype=GROUP_DOCTYPE,
        entity_name=identity_group,
        event_type="Revalidate",
        reason="governed_qc_investigation_resolution",
        nonce=investigation,
        from_status="Needs Revalidation",
        to_status="Active",
        identity_group=identity_group,
        metadata={"investigation": investigation},
    )
    return len(memberships)


def apply_qc_circuit_breaker(recommendation_name: str) -> dict[str, Any]:
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if (
        recommendation.qc_final_label != "Different"
        or recommendation.qc_review_status not in FINAL_STATUSES
    ):
        frappe.throw("Circuit breaker requires a finalized QC Different")
    if recommendation.qc_failure_action:
        return {
            "recommendation": recommendation.name,
            "status": "Already Applied",
            "action": recommendation.qc_failure_action,
        }

    _lock_named_rows(RECOMMENDATION_DOCTYPE, (recommendation.name,))
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation.name)
    shared_group = _current_shared_group(recommendation)
    scope = f"{recommendation.matching_policy}:{recommendation.source_pair}"
    reason = f"confirmed_qc_different:{recommendation.name}"
    investigation = _open_investigation(
        recommendation, scope, reason, shared_group
    )
    pause = _pause_automation(
        scope,
        reason,
        metadata={
            "recommendation": recommendation.name,
            "investigation": investigation,
            "current_shared_group": shared_group or "",
        },
    )
    suspended = _suspend_group_for_qc(shared_group, investigation)
    action = (
        f"automation_paused;investigation={investigation};"
        f"shared_group={shared_group or 'none'};"
        f"memberships_needing_revalidation={suspended};pause_event={pause['event']}"
    )
    frappe.db.set_value(
        RECOMMENDATION_DOCTYPE,
        recommendation.name,
        "qc_failure_action",
        action,
        update_modified=False,
    )
    return {
        "recommendation": recommendation.name,
        "status": "Paused",
        "scope": scope,
        "investigation": investigation,
        "current_shared_group": shared_group or "",
        "memberships_needing_revalidation": suspended,
        "pause_event": pause["event"],
    }


def _mark_stale_qc_rows(run_name: str) -> int:
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name, "qc_selected": 1, "qc_stale": 0},
        fields=[
            "name",
            "qc_review_status",
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
        ],
        limit_page_length=100_000,
    )
    if not rows:
        return 0
    record_ids = sorted(
        {
            str(record_id)
            for row in rows
            for record_id in (row.left_record, row.right_record)
        }
    )
    current_modified = {
        str(row.name): str(row.modified or "")
        for row in frappe.get_all(
            "CCD Master",
            filters={"name": ["in", record_ids]},
            fields=["name", "modified"],
            limit_page_length=max(len(record_ids), 1),
        )
    }
    stale = []
    for row in rows:
        # A completed QC decision is immutable historical evidence. Only an
        # unfinished case can become stale and be replaced by replenishment.
        if row.qc_review_status in FINAL_STATUSES:
            continue
        if (
            current_modified.get(str(row.left_record), "")
            != str(row.left_modified_at or "")
            or current_modified.get(str(row.right_record), "")
            != str(row.right_modified_at or "")
        ):
            stale.append(str(row.name))
    for name in stale:
        frappe.db.set_value(
            RECOMMENDATION_DOCTYPE,
            name,
            {"qc_stale": 1, "qc_review_status": "Stale"},
            update_modified=False,
        )
    return len(stale)


def _rolling_rows(run_name: str) -> list[Any]:
    return frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name, "qc_selected": 1, "qc_stale": 0},
        fields=[
            "name",
            "qc_review_status",
            "qc_final_label",
            "qc_due_at",
            "qc_assigned_at",
            "qc_finalized_at",
            "qc_failure_action",
        ],
        order_by="qc_finalized_at, qc_assigned_at, name",
        limit_page_length=100_000,
    )


def _precision_finalized_rows(rows: list[Any]) -> list[Any]:
    """Exclude only explicitly adjudged QC-review mistakes from precision."""
    names = [str(row.name) for row in rows if row.qc_review_status in FINAL_STATUSES]
    if not names:
        return []
    excluded = set(
        frappe.get_all(
            INVESTIGATION_DOCTYPE,
            filters={
                "recommendation": ["in", names],
                "status": "Resolved",
                "resolution_action": "QC Review Error",
            },
            pluck="recommendation",
            limit_page_length=max(len(names), 1),
        )
    )
    return [
        row
        for row in rows
        if row.qc_review_status in FINAL_STATUSES and str(row.name) not in excluded
    ]


def refresh_qc_monitor(run_name: str) -> dict[str, Any]:
    _lock_named_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    stale = _mark_stale_qc_rows(run.name)
    rows = _rolling_rows(run.name)
    finalized = [row for row in rows if row.qc_review_status in FINAL_STATUSES]
    precision_finalized = _precision_finalized_rows(rows)
    summary = rolling_qc_summary(
        [str(row.qc_final_label or "") for row in precision_finalized],
        max(int(settings.rolling_qc_window or 100), 1),
    )
    now = now_datetime()
    overdue = sum(
        bool(row.qc_due_at)
        and row.qc_review_status not in FINAL_STATUSES
        and row.qc_review_status != "Stale"
        and get_datetime(row.qc_due_at) < now
        for row in rows
    )
    failures = []
    for row in finalized:
        if row.qc_final_label == "Different" and not row.qc_failure_action:
            failures.append(apply_qc_circuit_breaker(row.name))
    if summary["window_complete"] and summary["wilson_95"][0] < PRECISION_LOWER_TARGET:
        _pause_automation(
            str(run.matching_policy),
            "rolling_qc_wilson_lower_below_0.95:"
            f"{summary['wilson_95'][0]:.6f}",
            metadata={"canary_run": run.name},
        )
    if overdue:
        _pause_automation(
            str(run.matching_policy),
            f"qc_sla_overdue:{overdue}",
            metadata={"canary_run": run.name},
        )
    state = (
        "Paused"
        if frappe.db.get_single_value(SETTINGS_DOCTYPE, "automation_paused")
        else "Monitoring"
    )
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            # Lifetime completion remains visible even after the precision
            # calculation advances beyond one rolling window.
            "qc_review_complete_count": len(finalized),
            "qc_same_count": summary["same"],
            "qc_different_count": summary["different"],
            "qc_precision_excluded_count": len(finalized)
            - len(precision_finalized),
            "qc_precision": summary["precision"],
            "qc_wilson_lower": summary["wilson_95"][0],
            "qc_wilson_upper": summary["wilson_95"][1],
            "qc_overdue_count": overdue,
            "qc_automation_state": state,
        },
        update_modified=False,
    )
    return {
        "run": run.name,
        **summary,
        "overdue": overdue,
        "stale": stale,
        "precision_excluded_review_errors": len(finalized)
        - len(precision_finalized),
        "automation_state": state,
        "new_failures": failures,
    }


def _replenishment_candidates(run_name: str) -> list[Any]:
    common = {
        "canary_run": run_name,
        "qc_selected": 0,
        "qc_stale": 0,
    }
    applied = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={**common, "status": "Approved", "rollout_state": "Applied"},
        fields=["name", "recommendation_key"],
        limit_page_length=100_000,
    )
    proposed = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={**common, "status": "Proposed", "rollout_state": "Available"},
        fields=["name", "recommendation_key"],
        limit_page_length=100_000,
    )
    return [*applied, *proposed]


def _replenish_qc_pool(run_name: str, needed: int) -> int:
    if needed <= 0:
        return 0
    selected = deterministic_qc_selection(
        run_name, _replenishment_candidates(run_name), needed
    )
    for name in selected:
        frappe.db.set_value(
            RECOMMENDATION_DOCTYPE,
            name,
            {
                "qc_selected": 1,
                "qc_review_status": "Unreviewed",
                "qc_final_label": None,
                "qc_stale": 0,
            },
            update_modified=False,
        )
    if selected:
        frappe.db.set_value(
            RUN_DOCTYPE,
            run_name,
            "qc_replenished_count",
            int(
                frappe.db.get_value(RUN_DOCTYPE, run_name, "qc_replenished_count")
                or 0
            )
            + len(selected),
            update_modified=False,
        )
    return len(selected)


def _assign_qc_cases(
    run_name: str,
    requested: int,
    *,
    automated: bool,
    advance_cadence: bool,
) -> dict[str, Any]:
    _lock_named_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status not in {"Ready", "Active"}:
        frappe.throw("QC cases can be scheduled only for a Ready or Active canary")
    if requested < 1 or requested > MAX_QC_ASSIGNMENT:
        frappe.throw(f"QC assignment count must be between 1 and {MAX_QC_ASSIGNMENT}")
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    available = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={
            "canary_run": run.name,
            "qc_selected": 1,
            "qc_stale": 0,
            "qc_assigned_at": ["is", "not set"],
            "qc_review_status": ["in", sorted(OPEN_REVIEW_STATUSES)],
        },
        fields=["name"],
        order_by="recommendation_key",
        limit=requested,
    )
    replenished = 0
    if len(available) < requested:
        replenished = _replenish_qc_pool(run.name, requested - len(available))
        available = frappe.get_all(
            RECOMMENDATION_DOCTYPE,
            filters={
                "canary_run": run.name,
                "qc_selected": 1,
                "qc_stale": 0,
                "qc_assigned_at": ["is", "not set"],
                "qc_review_status": ["in", sorted(OPEN_REVIEW_STATUSES)],
            },
            fields=["name"],
            order_by="recommendation_key",
            limit=requested,
        )
    now = now_datetime()
    due = add_days(now, int(settings.qc_sla_days or 14))
    for row in available:
        frappe.db.set_value(
            RECOMMENDATION_DOCTYPE,
            row.name,
            {"qc_assigned_at": now, "qc_due_at": due},
            update_modified=False,
        )
    if advance_cadence:
        interval = max(int(settings.qc_assignment_interval_days or 7), 1)
        values = {
            "qc_last_assignment_at": now,
            "qc_next_assignment_at": add_days(now, interval),
            "qc_assignment_cycle_count": int(run.qc_assignment_cycle_count or 0) + 1,
        }
        frappe.db.set_value(RUN_DOCTYPE, run.name, values, update_modified=False)
    from db_connector.api_fuzzy_canary import _refresh_review_workflow_counts

    _refresh_review_workflow_counts(run.name)
    event = _append_event(
        entity_doctype=RUN_DOCTYPE,
        entity_name=run.name,
        event_type="QC Assign",
        reason="automatic_qc_cadence" if automated else "manager_qc_assignment",
        nonce=hashlib.sha256(
            f"{run.name}\x1f{now}\x1f{len(available)}\x1f{automated}".encode()
        ).hexdigest(),
        from_status="Pending",
        to_status="Assigned",
        metadata={
            "assigned": len(available),
            "replenished": replenished,
            "due_at": due,
            "automatic": automated,
        },
    )
    summary = refresh_qc_monitor(run.name)
    return {
        "run": run.name,
        "assigned": len(available),
        "replenished": replenished,
        "due_at": due,
        "assignment_event": event,
        **summary,
    }


@frappe.whitelist()
def assign_qc_cases(run_name: str, count: int | str | None = None) -> dict[str, Any]:
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    requested = int(
        count if count not in (None, "") else settings.qc_cases_per_week or 10
    )
    result = _assign_qc_cases(
        run_name, requested, automated=False, advance_cadence=True
    )
    frappe.db.commit()
    return result


def run_qc_cadence() -> dict[str, Any]:
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if not settings.automatic_qc_assignment_enabled:
        return {"status": "Disabled", "assigned": 0}
    run_name = str(settings.automatic_tiered_canary or "")
    if not run_name:
        pause = _pause_automation(
            "global", "automatic_qc_canary_not_configured"
        )
        return {"status": "Paused", "assigned": 0, **pause}
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    next_at = get_datetime(run.qc_next_assignment_at) if run.qc_next_assignment_at else None
    now = now_datetime()
    if not cadence_due(next_at, now):
        return {
            "status": "Not Due",
            "run": run.name,
            "assigned": 0,
            "next_assignment_at": next_at,
        }
    result = _assign_qc_cases(
        run.name,
        max(int(settings.qc_cases_per_week or 10), 1),
        automated=True,
        advance_cadence=True,
    )
    if result["assigned"] == 0 and settings.automatic_tiered_enabled:
        pause = _pause_automation(
            str(run.matching_policy),
            "qc_cadence_no_eligible_cases",
            metadata={"canary_run": run.name},
        )
        result.update({"status": "Paused", **pause})
    else:
        result["status"] = "Assigned"
    return result


def _resume_blockers() -> list[str]:
    blockers = []
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.automatic_tiered_enabled:
        blockers.append("disable_automatic_tiered_before_resume")
    open_investigations = frappe.db.count(INVESTIGATION_DOCTYPE, {"status": "Open"})
    if open_investigations:
        blockers.append(f"open_qc_investigations:{open_investigations}")
    now = now_datetime()
    overdue = frappe.db.count(
        RECOMMENDATION_DOCTYPE,
        {
            "qc_selected": 1,
            "qc_stale": 0,
            "qc_due_at": ["<", now],
            "qc_review_status": ["not in", sorted(FINAL_STATUSES | {"Stale"})],
        },
    )
    if overdue:
        blockers.append(f"overdue_qc_cases:{overdue}")
    authorized_policy = str(settings.automatic_tiered_policy or "")
    for investigation in frappe.get_all(
        INVESTIGATION_DOCTYPE,
        filters={
            "status": "Resolved",
            "resolution_action": ["in", ["Relationship Corrected", "Policy Disabled"]],
        },
        fields=["name", "recommendation", "resolution_action"],
        limit_page_length=10_000,
    ):
        affected_policy = str(
            frappe.db.get_value(
                RECOMMENDATION_DOCTYPE, investigation.recommendation, "matching_policy"
            )
            or ""
        )
        if not authorized_policy or affected_policy != authorized_policy:
            continue
        policy_status = str(
            frappe.db.get_value(
                "CCD Matching Policy", affected_policy, "status"
            )
            or ""
        )
        if policy_status == "Pilot":
            blockers.append(
                f"qc_failure_policy_still_pilot:{investigation.name}:{affected_policy}"
            )
    window_size = max(int(settings.rolling_qc_window or 100), 1)
    for run in frappe.get_all(
        RUN_DOCTYPE,
        filters={"status": ["in", ["Ready", "Active"]], "qc_sample_count": [">", 0]},
        fields=["name"],
        limit_page_length=100,
    ):
        finalized = [
            str(row.qc_final_label or "")
            for row in _precision_finalized_rows(_rolling_rows(run.name))
        ]
        summary = rolling_qc_summary(finalized, window_size)
        if summary["window_complete"] and summary["wilson_95"][0] < PRECISION_LOWER_TARGET:
            blockers.append(
                f"rolling_precision_failure:{run.name}:{summary['wilson_95'][0]:.6f}"
            )
    return sorted(blockers)


@frappe.whitelist()
def preview_resume_tiered_automation() -> dict[str, Any]:
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    blockers = _resume_blockers()
    return {
        "zero_write": True,
        "currently_paused": bool(settings.automation_paused),
        "pause_scope": str(settings.pause_scope or ""),
        "pause_reason": str(settings.pause_reason or ""),
        "eligible": not blockers,
        "blockers": blockers,
    }


@frappe.whitelist()
def pause_tiered_automation(
    reason: str, confirm_settings_name: str
) -> dict[str, Any]:
    _require_manager()
    if str(confirm_settings_name or "").strip() != SETTINGS_DOCTYPE:
        frappe.throw("Type the exact Settings ID to confirm pause")
    reason = str(reason or "").strip()
    if not reason:
        frappe.throw("A pause reason is required")
    result = _pause_automation(
        "global", f"manager_pause:{reason}", metadata={"manager_reason": reason}
    )
    frappe.db.commit()
    return {"status": "Paused", **result}


@frappe.whitelist()
def resolve_qc_investigation(
    investigation_name: str,
    resolution_action: str,
    notes: str,
    confirm_investigation_name: str,
) -> dict[str, Any]:
    _require_manager()
    investigation_name = str(investigation_name or "").strip()
    if str(confirm_investigation_name or "").strip() != investigation_name:
        frappe.throw("Type the exact QC Investigation ID to confirm resolution")
    action = str(resolution_action or "").strip()
    allowed = {
        "Relationship Corrected",
        "Relationship Revalidated",
        "QC Review Error",
        "Policy Disabled",
    }
    if action not in allowed:
        frappe.throw("Select a valid investigation resolution action")
    notes = str(notes or "").strip()
    if not notes:
        frappe.throw("Resolution notes are required")
    _lock_named_rows(INVESTIGATION_DOCTYPE, (investigation_name,))
    investigation = frappe.get_doc(INVESTIGATION_DOCTYPE, investigation_name)
    if investigation.status == "Resolved":
        return {
            "investigation": investigation.name,
            "status": "Already Resolved",
            "resolution_event": investigation.resolution_event or "",
        }
    if investigation.status != "Open":
        frappe.throw("Only an Open QC Investigation may be resolved")
    revalidated_memberships = 0
    if investigation.identity_group:
        group_status = frappe.db.get_value(
            GROUP_DOCTYPE, investigation.identity_group, "status"
        )
        if group_status == "Needs Revalidation" and action in {
            "Relationship Revalidated",
            "QC Review Error",
        }:
            revalidated_memberships = _revalidate_investigation_group(
                str(investigation.identity_group), investigation.name
            )
        elif group_status == "Needs Revalidation":
            frappe.throw(
                "Correct or revalidate the affected Identity Group before resolving this investigation"
            )
    now = now_datetime()
    event = _append_event(
        entity_doctype=INVESTIGATION_DOCTYPE,
        entity_name=investigation.name,
        event_type="Resolve",
        reason=notes,
        nonce=hashlib.sha256(
            f"{investigation.name}\x1f{action}\x1f{notes}".encode()
        ).hexdigest(),
        from_status="Open",
        to_status="Resolved",
        identity_decision=investigation.identity_decision or "",
        identity_group=investigation.identity_group or "",
        metadata={
            "resolution_action": action,
            "revalidated_memberships": revalidated_memberships,
        },
    )
    frappe.db.set_value(
        INVESTIGATION_DOCTYPE,
        investigation.name,
        {
            "status": "Resolved",
            "resolution_action": action,
            "resolved_at": now,
            "resolved_by": frappe.session.user,
            "resolution_notes": notes,
            "resolution_event": event,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "investigation": investigation.name,
        "status": "Resolved",
        "resolution_event": event,
        "revalidated_memberships": revalidated_memberships,
    }


@frappe.whitelist()
def resume_tiered_automation(
    reason: str, confirm_settings_name: str
) -> dict[str, Any]:
    _require_manager()
    if str(confirm_settings_name or "").strip() != SETTINGS_DOCTYPE:
        frappe.throw("Type the exact Settings ID to confirm resume")
    reason = str(reason or "").strip()
    if not reason:
        frappe.throw("A resume reason is required")
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    blockers = _resume_blockers()
    if blockers:
        frappe.throw("Tiered automation cannot resume: " + ", ".join(blockers))
    if not settings.automation_paused:
        return {"status": "Already Clear", "event": ""}
    revision = int(settings.automation_control_revision or 0) + 1
    prior_scope = str(settings.pause_scope or "")
    prior_reason = str(settings.pause_reason or "")
    _set_single_values(
        {
            "automation_paused": 0,
            "pause_scope": None,
            "pause_reason": None,
            "automation_control_revision": revision,
        }
    )
    event = _append_control_event(
        event_type="Resume",
        action="tiered_circuit_breaker",
        revision=revision,
        reason=reason,
        metadata={
            "from_status": "Paused",
            "to_status": "Monitoring",
            "prior_scope": prior_scope,
            "prior_reason": prior_reason,
        },
    )
    frappe.db.commit()
    return {"status": "Monitoring", "event": event, "revision": revision}


@frappe.whitelist()
def set_automatic_qc_assignment(
    enabled: int | str, reason: str, confirm_settings_name: str
) -> dict[str, Any]:
    _require_manager()
    if str(confirm_settings_name or "").strip() != SETTINGS_DOCTYPE:
        frappe.throw("Type the exact Settings ID to confirm this control change")
    reason = str(reason or "").strip()
    if not reason:
        frappe.throw("A control-change reason is required")
    desired = str(enabled or "0").strip().casefold() in {"1", "true", "yes", "on"}
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if desired and not settings.automatic_tiered_canary:
        frappe.throw("Select the authorized Tiered Canary before enabling QC cadence")
    revision = int(settings.automation_control_revision or 0) + 1
    values: dict[str, Any] = {
        "automatic_qc_assignment_enabled": int(desired),
        "automation_control_revision": revision,
    }
    if not desired and settings.automatic_tiered_enabled:
        values["automatic_tiered_enabled"] = 0
        values["automatic_tiered_authorization_event"] = None
        values["last_automatic_status"] = "Disabled by QC control"
    _set_single_values(values)
    event = _append_control_event(
        event_type="Enable" if desired else "Disable",
        action="automatic_qc_assignment",
        revision=revision,
        reason=reason,
        metadata={
            "from_status": "Enabled" if settings.automatic_qc_assignment_enabled else "Disabled",
            "to_status": "Enabled" if desired else "Disabled",
            "automatic_tiered_also_disabled": bool(
                not desired and settings.automatic_tiered_enabled
            ),
        },
    )
    if not desired and settings.automatic_tiered_enabled:
        _pause_automation(
            "global",
            "automatic_qc_assignment_disabled",
            metadata={"control_event": event},
        )
    frappe.db.commit()
    return {
        "status": "Enabled" if desired else "Disabled",
        "event": event,
        "automatic_tiered_enabled": bool(desired and settings.automatic_tiered_enabled),
    }


def run_qc_monitor() -> None:
    if not frappe.db.table_exists(RUN_DOCTYPE):
        return
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.automatic_qc_assignment_enabled:
        run_qc_cadence()
    runs = frappe.get_all(
        RUN_DOCTYPE,
        filters={"status": ["in", ["Ready", "Active"]], "qc_sample_count": [">", 0]},
        pluck="name",
        limit_page_length=100,
    )
    for run_name in runs:
        refresh_qc_monitor(run_name)
    frappe.db.commit()

    # Keep the existing daily scheduler hook as the single orchestration point.
    # The automatic worker has its own default-off controls and fresh lock-time
    # checks, so importing it here cannot create identity objects by itself.
    from db_connector.api_identity_automation import run_automatic_tiered_cycle

    run_automatic_tiered_cycle(scheduled=True)
