"""Asynchronous QC cadence, rolling precision, and fail-closed circuit breakers."""

from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import add_days, now_datetime

from db_connector.api_identity_resolution import (
    CURRENT_MEMBERSHIP_STATUSES,
    _append_event,
)
from db_connector.fuzzy_matching.metrics import wilson_interval

RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
MEMBERSHIP_DOCTYPE = "CCD Identity Membership"
GROUP_DOCTYPE = "CCD Identity Group"
FINAL_STATUSES = {"Agreed", "Adjudicated"}


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _pause_automation(scope: str, reason: str) -> None:
    frappe.db.set_single_value(SETTINGS_DOCTYPE, "automation_paused", 1)
    frappe.db.set_single_value(SETTINGS_DOCTYPE, "pause_scope", scope)
    frappe.db.set_single_value(SETTINGS_DOCTYPE, "pause_reason", reason)


def _open_investigation(recommendation: Any, scope: str, reason: str) -> str:
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
            "identity_group": recommendation.identity_group or None,
            "pause_scope": scope,
            "status": "Open",
            "reason": reason,
            "opened_at": now_datetime(),
            "opened_by": frappe.session.user or "Administrator",
        }
    ).insert(ignore_permissions=True)
    return doc.name


def _suspend_group_for_qc(recommendation: Any, investigation: str) -> int:
    if not recommendation.identity_group:
        return 0
    memberships = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "identity_group": recommendation.identity_group,
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=["name", "status", "originating_decision"],
        limit_page_length=100_000,
    )
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
            identity_group=recommendation.identity_group,
            identity_membership=membership.name,
            metadata={"investigation": investigation},
        )
    frappe.db.set_value(
        GROUP_DOCTYPE,
        recommendation.identity_group,
        "status",
        "Needs Revalidation",
        update_modified=False,
    )
    return len(memberships)


def apply_qc_circuit_breaker(recommendation_name: str) -> dict[str, Any]:
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if recommendation.qc_final_label != "Different" or recommendation.qc_review_status not in FINAL_STATUSES:
        frappe.throw("Circuit breaker requires a finalized QC Different")
    if recommendation.qc_failure_action:
        return {
            "recommendation": recommendation.name,
            "status": "Already Applied",
            "action": recommendation.qc_failure_action,
        }
    scope = f"{recommendation.matching_policy}:{recommendation.source_pair}"
    reason = f"confirmed_qc_different:{recommendation.name}"
    _pause_automation(scope, reason)
    investigation = _open_investigation(recommendation, scope, reason)
    suspended = _suspend_group_for_qc(recommendation, investigation)
    action = (
        f"automation_paused;investigation={investigation};"
        f"memberships_needing_revalidation={suspended}"
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
        "memberships_needing_revalidation": suspended,
    }


def refresh_qc_monitor(run_name: str) -> dict[str, Any]:
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run.name, "qc_selected": 1},
        fields=[
            "name",
            "qc_review_status",
            "qc_final_label",
            "qc_due_at",
            "qc_failure_action",
        ],
        order_by="qc_assigned_at, name",
        limit_page_length=100_000,
    )
    finalized = [row for row in rows if row.qc_review_status in FINAL_STATUSES]
    window_size = max(int(settings.rolling_qc_window or 100), 1)
    comparable = finalized[-window_size:]
    same = sum(row.qc_final_label == "Same" for row in comparable)
    different = sum(row.qc_final_label == "Different" for row in comparable)
    total = same + different
    precision = same / total if total else 0.0
    lower, upper = wilson_interval(same, total)
    now = now_datetime()
    overdue = sum(
        bool(row.qc_due_at)
        and row.qc_review_status not in FINAL_STATUSES
        and row.qc_review_status != "Stale"
        and row.qc_due_at < now
        for row in rows
    )
    failures = []
    for row in finalized:
        if row.qc_final_label == "Different" and not row.qc_failure_action:
            failures.append(apply_qc_circuit_breaker(row.name))
    if total >= window_size and lower < 0.95:
        _pause_automation(
            str(run.matching_policy),
            f"rolling_qc_wilson_lower_below_0.95:{lower:.6f}",
        )
    if overdue:
        _pause_automation(
            str(run.matching_policy),
            f"qc_sla_overdue:{overdue}",
        )
    state = "Paused" if frappe.db.get_single_value(SETTINGS_DOCTYPE, "automation_paused") else "Monitoring"
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            "qc_same_count": same,
            "qc_different_count": different,
            "qc_precision": precision,
            "qc_wilson_lower": lower,
            "qc_wilson_upper": upper,
            "qc_overdue_count": overdue,
            "qc_automation_state": state,
        },
        update_modified=False,
    )
    return {
        "run": run.name,
        "window_finalized": total,
        "same": same,
        "different": different,
        "precision": precision,
        "wilson_95": [lower, upper],
        "overdue": overdue,
        "automation_state": state,
        "new_failures": failures,
    }


@frappe.whitelist()
def assign_qc_cases(run_name: str, count: int | str | None = None) -> dict[str, Any]:
    _require_manager()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status not in {"Ready", "Active"}:
        frappe.throw("QC cases can be scheduled only for a Ready or Active canary")
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    requested = int(count if count not in (None, "") else settings.qc_cases_per_week or 10)
    if requested <= 0:
        frappe.throw("QC assignment count must be greater than zero")
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={
            "canary_run": run.name,
            "qc_selected": 1,
            "qc_assigned_at": ["is", "not set"],
            "qc_review_status": ["in", ["Unreviewed", "Partially Reviewed", "Positive Confirmation Required", "Needs Adjudication"]],
        },
        fields=["name"],
        order_by="recommendation_key",
        limit=requested,
    )
    now = now_datetime()
    due = add_days(now, int(settings.qc_sla_days or 14))
    for row in rows:
        frappe.db.set_value(
            RECOMMENDATION_DOCTYPE,
            row.name,
            {"qc_assigned_at": now, "qc_due_at": due},
            update_modified=False,
        )
    summary = refresh_qc_monitor(run.name)
    frappe.db.commit()
    return {
        "run": run.name,
        "assigned": len(rows),
        "due_at": due,
        **summary,
    }


def run_qc_monitor() -> None:
    if not frappe.db.table_exists(RUN_DOCTYPE):
        return
    runs = frappe.get_all(
        RUN_DOCTYPE,
        filters={"status": ["in", ["Ready", "Active"]], "qc_sample_count": [">", 0]},
        pluck="name",
        limit_page_length=100,
    )
    for run_name in runs:
        refresh_qc_monitor(run_name)
    frappe.db.commit()
