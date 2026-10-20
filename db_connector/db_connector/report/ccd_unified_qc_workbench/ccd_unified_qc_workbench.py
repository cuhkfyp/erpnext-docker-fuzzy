"""One read-only Desk queue for every assigned unattended-automation QC case."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime


SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
CANARY_DOCTYPE = "CCD Match Canary Run"
LABEL_DOCTYPE = "CCD Match Review Label"

CHANNEL_TIERED_ACTIVE = "Tiered — Active Canary"
CHANNEL_TIERED_BACKLOG = "Tiered — Mandatory Backlog"
CHANNEL_SPLINK = "Ultra-high Splink"
CHANNEL_HYBRID = "Development Hybrid"
CHANNELS = {
    CHANNEL_TIERED_ACTIVE,
    CHANNEL_TIERED_BACKLOG,
    CHANNEL_SPLINK,
    CHANNEL_HYBRID,
}
WORK_STATES = {
    "Pending",
    "All Assigned",
    "Completed",
    "Needs Adjudication",
    "Overdue",
    "Stale",
}
REVIEWER_SCOPES = {"All Cases", "Needs My Review", "Reviewed by Me"}
PENDING_STATUSES = {
    "Unreviewed",
    "Partially Reviewed",
    "Positive Confirmation Required",
    "Needs Adjudication",
}
FINAL_STATUSES = {"Agreed", "Adjudicated"}
MAXIMUM_ROWS = 5_000


def execute(filters: dict[str, Any] | None = None):
    _require_reader()
    normalized = _normalized_filters(filters or {})
    active_canary = str(
        frappe.db.get_single_value(SETTINGS_DOCTYPE, "automatic_tiered_canary")
        or ""
    )
    cases = _assigned_cases(active_canary)
    labels = _review_labels(cases)
    current_user = str(frappe.session.user)
    roles = set(frappe.get_roles())
    current_time = now_datetime()

    rows = [
        _enrich_case(
            case,
            labels.get((case["reference_doctype"], case["qc_case"]), []),
            current_user=current_user,
            can_adjudicate="System Manager" in roles,
            current_time=current_time,
        )
        for case in cases
    ]
    rows = [row for row in rows if _matches_filters(row, normalized)]
    rows.sort(key=_sort_key)
    matching_count = len(rows)
    visible_rows = rows[: normalized.maximum_rows]
    message = _message(visible_rows, matching_count, normalized)
    return (
        _columns(),
        visible_rows,
        message,
        None,
        _report_summary(rows, current_user),
    )


def _require_reader() -> None:
    roles = set(frappe.get_roles())
    permitted = {
        "System Manager",
        "CCD Match Reviewer",
        "CCD Match Sensitive Reviewer",
    }
    if not roles & permitted:
        frappe.throw(_("CCD Match Reviewer role is required"), frappe.PermissionError)


def _normalized_filters(filters: dict[str, Any]) -> frappe._dict:
    output = frappe._dict(filters)
    output.work_state = str(output.get("work_state") or "Pending")
    output.channel = str(output.get("channel") or "")
    output.reviewer_scope = str(output.get("reviewer_scope") or "All Cases")
    if output.work_state not in WORK_STATES:
        frappe.throw(_("Unknown QC Work State filter"))
    if output.channel and output.channel not in CHANNELS:
        frappe.throw(_("Unknown QC Channel filter"))
    if output.reviewer_scope not in REVIEWER_SCOPES:
        frappe.throw(_("Unknown My Review filter"))

    output.assigned_from = (
        getdate(output.get("assigned_from")) if output.get("assigned_from") else None
    )
    output.assigned_to = (
        getdate(output.get("assigned_to")) if output.get("assigned_to") else None
    )
    if cint(output.get("this_week_only")):
        today = getdate()
        week_start = add_days(today, -today.weekday())
        output.assigned_from = max(
            filter(None, (output.assigned_from, week_start)), default=week_start
        )
    if (
        output.assigned_from
        and output.assigned_to
        and output.assigned_to < output.assigned_from
    ):
        frappe.throw(_("Assigned To cannot be earlier than Assigned From"))

    output.maximum_rows = cint(output.get("maximum_rows") or 500)
    if output.maximum_rows < 1 or output.maximum_rows > MAXIMUM_ROWS:
        frappe.throw(_("Maximum Rows must be between 1 and {0}").format(MAXIMUM_ROWS))
    return output


def _assigned_cases(active_canary: str) -> list[dict[str, Any]]:
    recommendations = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"qc_selected": 1, "qc_assigned_at": ["is", "set"]},
        fields=[
            "name",
            "canary_run",
            "source_pair",
            "activation_batch",
            "qc_assigned_at",
            "qc_due_at",
            "qc_review_status",
            "qc_final_label",
            "qc_stale",
        ],
        limit_page_length=100_000,
    )
    canary_names = sorted({str(row.canary_run) for row in recommendations})
    canary_statuses: dict[str, str] = {}
    if canary_names:
        canary_statuses = {
            str(row.name): str(row.status or "")
            for row in frappe.get_all(
                CANARY_DOCTYPE,
                filters={"name": ["in", canary_names]},
                fields=["name", "status"],
                limit_page_length=len(canary_names),
            )
        }

    rows: list[dict[str, Any]] = []
    for recommendation in recommendations:
        canary = str(recommendation.canary_run)
        active = bool(active_canary and canary == active_canary)
        rows.append(
            {
                "channel": CHANNEL_TIERED_ACTIVE if active else CHANNEL_TIERED_BACKLOG,
                "reference_doctype": RECOMMENDATION_DOCTYPE,
                "qc_case": str(recommendation.name),
                "governance_doctype": CANARY_DOCTYPE,
                "governance_reference": canary,
                "governance_scope": canary_statuses.get(canary) or _("Unknown status"),
                "source_pair": str(recommendation.source_pair or ""),
                "assigned_at": recommendation.qc_assigned_at,
                "due_at": recommendation.qc_due_at,
                "review_status": str(recommendation.qc_review_status or "Unreviewed"),
                "final_label": str(recommendation.qc_final_label or ""),
                "stale": cint(recommendation.qc_stale),
                "batch_doctype": (
                    "CCD Identity Activation Batch" if recommendation.activation_batch else ""
                ),
                "batch": str(recommendation.activation_batch or ""),
            }
        )

    candidates = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={"automated_qc_assigned_at": ["is", "set"]},
        fields=[
            "name",
            "source_pair",
            "automation_channel",
            "automation_validation_run",
            "automation_cohort",
            "automation_batch",
            "automated_qc_assigned_at",
            "automated_qc_due_at",
            "automated_qc_review_status",
            "automated_qc_final_label",
            "automated_qc_stale",
        ],
        limit_page_length=100_000,
    )
    for candidate in candidates:
        hybrid = str(candidate.automation_channel or "") == "Hybrid Development"
        validation_run = str(candidate.automation_validation_run or "")
        rows.append(
            {
                "channel": CHANNEL_HYBRID if hybrid else CHANNEL_SPLINK,
                "reference_doctype": CANDIDATE_DOCTYPE,
                "qc_case": str(candidate.name),
                "governance_doctype": (
                    "CCD Match Evaluation Run" if validation_run else ""
                ),
                "governance_reference": validation_run,
                "governance_scope": str(candidate.automation_cohort or ""),
                "source_pair": str(candidate.source_pair or ""),
                "assigned_at": candidate.automated_qc_assigned_at,
                "due_at": candidate.automated_qc_due_at,
                "review_status": str(
                    candidate.automated_qc_review_status or "Unreviewed"
                ),
                "final_label": str(candidate.automated_qc_final_label or ""),
                "stale": cint(candidate.automated_qc_stale),
                "batch_doctype": (
                    "CCD Splink Automation Batch" if candidate.automation_batch else ""
                ),
                "batch": str(candidate.automation_batch or ""),
            }
        )
    return rows


def _review_labels(cases: Iterable[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    case_names: dict[str, list[str]] = {
        RECOMMENDATION_DOCTYPE: [],
        CANDIDATE_DOCTYPE: [],
    }
    for case in cases:
        case_names[case["reference_doctype"]].append(case["qc_case"])

    labels: dict[tuple[str, str], list[dict[str, Any]]] = {}
    parent_fields = {
        RECOMMENDATION_DOCTYPE: "qc_review_labels",
        CANDIDATE_DOCTYPE: "automated_qc_review_labels",
    }
    for parenttype, names in case_names.items():
        for offset in range(0, len(names), 500):
            chunk = names[offset : offset + 500]
            if not chunk:
                continue
            for label in frappe.get_all(
                LABEL_DOCTYPE,
                filters={
                    "parenttype": parenttype,
                    "parentfield": parent_fields[parenttype],
                    "parent": ["in", chunk],
                },
                fields=["parent", "reviewer", "is_adjudication"],
                limit_page_length=2_000,
            ):
                labels.setdefault((parenttype, str(label.parent)), []).append(
                    {
                        "reviewer": str(label.reviewer),
                        "is_adjudication": cint(label.is_adjudication),
                    }
                )
    return labels


def _enrich_case(
    case: dict[str, Any],
    labels: Iterable[dict[str, Any]],
    *,
    current_user: str,
    can_adjudicate: bool,
    current_time: datetime,
) -> dict[str, Any]:
    output = dict(case)
    labels = list(labels)
    ordinary = [label for label in labels if not label["is_adjudication"]]
    adjudications = [label for label in labels if label["is_adjudication"]]
    reviewed_by_me = any(label["reviewer"] == current_user for label in labels)
    ordinary_reviewed_by_me = any(
        label["reviewer"] == current_user for label in ordinary
    )
    status = output["review_status"]
    stale = bool(output["stale"] or status == "Stale")
    complete = status in FINAL_STATUSES
    pending = status in PENDING_STATUSES and not stale
    due_at = get_datetime(output["due_at"]) if output.get("due_at") else None
    overdue = bool(pending and due_at and due_at < current_time)

    if stale:
        my_action = _("Stale — do not review")
    elif complete:
        my_action = _("Complete")
    elif status == "Needs Adjudication":
        my_action = _("Adjudicate") if can_adjudicate else _("Awaiting adjudication")
    elif not ordinary_reviewed_by_me:
        my_action = _("Review now")
    else:
        my_action = _("Awaiting another reviewer")

    progress = _("{0} / 2").format(len(ordinary))
    if adjudications:
        progress = _("{0}; adjudicated").format(progress)
    output.update(
        {
            "review_progress": progress,
            "reviewed_by_me": reviewed_by_me,
            "my_action": my_action,
            "pending": pending,
            "complete": complete,
            "overdue": overdue,
            "stale": stale,
        }
    )
    return output


def _matches_filters(row: dict[str, Any], filters: frappe._dict) -> bool:
    if filters.channel and row["channel"] != filters.channel:
        return False
    state = filters.work_state
    if state == "Pending" and not row["pending"]:
        return False
    if state == "Completed" and not row["complete"]:
        return False
    if state == "Needs Adjudication" and row["review_status"] != "Needs Adjudication":
        return False
    if state == "Overdue" and not row["overdue"]:
        return False
    if state == "Stale" and not row["stale"]:
        return False

    if filters.reviewer_scope == "Needs My Review" and row["my_action"] not in {
        _("Review now"),
        _("Adjudicate"),
    }:
        return False
    if filters.reviewer_scope == "Reviewed by Me" and not row["reviewed_by_me"]:
        return False

    assigned_date = getdate(row["assigned_at"])
    if filters.assigned_from and assigned_date < filters.assigned_from:
        return False
    if filters.assigned_to and assigned_date > filters.assigned_to:
        return False
    return True


def _sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    status_priority = {
        "Needs Adjudication": 0,
        "Positive Confirmation Required": 1,
        "Partially Reviewed": 2,
        "Unreviewed": 3,
        "Agreed": 4,
        "Adjudicated": 4,
        "Stale": 5,
    }
    due_at = get_datetime(row["due_at"]) if row.get("due_at") else datetime.max
    assigned_at = (
        get_datetime(row["assigned_at"]) if row.get("assigned_at") else datetime.min
    )
    return (
        0 if row["overdue"] else 1,
        status_priority.get(row["review_status"], 9),
        due_at,
        -assigned_at.timestamp(),
        row["channel"],
        row["qc_case"],
    )


def _columns() -> list[dict[str, Any]]:
    return [
        {"fieldname": "channel", "label": _("QC Channel"), "fieldtype": "Data", "width": 205},
        {
            "fieldname": "qc_case",
            "label": _("Open Review"),
            "fieldtype": "Dynamic Link",
            "options": "reference_doctype",
            "width": 180,
        },
        {"fieldname": "reference_doctype", "label": _("Reference DocType"), "fieldtype": "Data", "hidden": 1},
        {
            "fieldname": "governance_reference",
            "label": _("Governance Reference"),
            "fieldtype": "Dynamic Link",
            "options": "governance_doctype",
            "width": 155,
        },
        {"fieldname": "governance_doctype", "label": _("Governance DocType"), "fieldtype": "Data", "hidden": 1},
        {"fieldname": "governance_scope", "label": _("Run/Cohort State"), "fieldtype": "Data", "width": 150},
        {"fieldname": "source_pair", "label": _("Source Pair"), "fieldtype": "Data", "width": 180},
        {"fieldname": "assigned_at", "label": _("Assigned At"), "fieldtype": "Datetime", "width": 155},
        {"fieldname": "due_at", "label": _("Due At"), "fieldtype": "Datetime", "width": 155},
        {"fieldname": "review_status", "label": _("QC Status"), "fieldtype": "Data", "width": 190},
        {"fieldname": "review_progress", "label": _("Reviews"), "fieldtype": "Data", "width": 105},
        {"fieldname": "my_action", "label": _("My Action"), "fieldtype": "Data", "width": 175},
        {"fieldname": "final_label", "label": _("Final Label"), "fieldtype": "Data", "width": 90},
        {"fieldname": "overdue", "label": _("Overdue"), "fieldtype": "Check", "width": 75},
        {
            "fieldname": "batch",
            "label": _("Automation Batch"),
            "fieldtype": "Dynamic Link",
            "options": "batch_doctype",
            "width": 130,
        },
        {"fieldname": "batch_doctype", "label": _("Batch DocType"), "fieldtype": "Data", "hidden": 1},
    ]


def _message(
    visible_rows: list[dict[str, Any]],
    matching_count: int,
    filters: frappe._dict,
) -> str:
    channel_counts = Counter(row["channel"] for row in visible_rows)
    counts = ", ".join(
        _("{0}: {1}").format(channel, channel_counts.get(channel, 0))
        for channel in (
            CHANNEL_TIERED_ACTIVE,
            CHANNEL_TIERED_BACKLOG,
            CHANNEL_SPLINK,
            CHANNEL_HYBRID,
        )
    )
    prefix = _(
        "Read-only unified queue. Open the QC Case to use the existing masked review workflow."
    )
    if matching_count > len(visible_rows):
        prefix += " " + _("Showing {0} of {1} matching cases.").format(
            len(visible_rows), matching_count
        )
    if filters.work_state == "Pending":
        prefix += " " + _("Unassigned and Not Selected candidates are intentionally excluded.")
    return _("{0} Visible channel counts — {1}").format(prefix, counts)


def _report_summary(rows: list[dict[str, Any]], current_user: str) -> list[dict[str, Any]]:
    return [
        {
            "value": len(rows),
            "label": _("Matching QC cases"),
            "datatype": "Int",
            "indicator": "Blue",
        },
        {
            "value": sum(row["pending"] for row in rows),
            "label": _("Pending"),
            "datatype": "Int",
            "indicator": "Orange",
        },
        {
            "value": sum(
                row["my_action"] in {_("Review now"), _("Adjudicate")} for row in rows
            ),
            "label": _("Needs my action"),
            "datatype": "Int",
            "indicator": "Blue",
        },
        {
            "value": sum(row["overdue"] for row in rows),
            "label": _("Overdue"),
            "datatype": "Int",
            "indicator": "Red",
        },
        {
            "value": sum(row["review_status"] == "Needs Adjudication" for row in rows),
            "label": _("Needs adjudication"),
            "datatype": "Int",
            "indicator": "Orange",
        },
    ]
