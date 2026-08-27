"""Read-only operational register for current CCD identity state."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Iterable

import frappe
from frappe import _
from frappe.utils import cint

from db_connector.api_fuzzy_evaluation import REVIEW_ROLE, SENSITIVE_ROLE
from db_connector.api_identity_resolution import CURRENT_MEMBERSHIP_STATUSES
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.policy import MatchingPolicy


MEMBERSHIP_DOCTYPE = "CCD Identity Membership"
GROUP_DOCTYPE = "CCD Identity Group"
DECISION_DOCTYPE = "CCD Identity Decision"
EXCLUSION_DOCTYPE = "CCD Identity Exclusion"
ALLOWED_STATES = {
    "Any Resolved",
    "Linked",
    "Needs Revalidation",
    "Resolved Separately",
}
MAXIMUM_LIMIT = 5_000


def execute(filters: dict[str, Any] | None = None):
    _require_reader()
    normalized = _normalized_filters(filters or {})
    rows = _current_resolution_rows()
    rows = _apply_filters(rows, normalized)
    matching_count = len(rows)
    rows = rows[: normalized.limit]
    message = None
    if matching_count > len(rows):
        message = _(
            "Showing {0} of {1} matching CCD Masters. Increase Maximum Rows "
            "or narrow the filters."
        ).format(len(rows), matching_count)
    return (
        _columns(),
        rows,
        message,
        None,
        _report_summary(rows, matching_count),
    )


def _require_reader() -> None:
    roles = set(frappe.get_roles())
    if not ({"System Manager", REVIEW_ROLE, SENSITIVE_ROLE} & roles):
        frappe.throw(_("CCD Match Reviewer role is required"), frappe.PermissionError)
    if not frappe.has_permission("CCD Master", "read"):
        frappe.throw(_("You cannot read CCD Master"), frappe.PermissionError)


def _normalized_filters(filters: dict[str, Any]) -> frappe._dict:
    output = frappe._dict(filters)
    output.identity_state = str(output.get("identity_state") or "Any Resolved")
    if output.identity_state not in ALLOWED_STATES:
        frappe.throw(_("Unknown Identity State filter"))
    output.min_group_members = max(cint(output.get("min_group_members")), 0)
    output.max_group_members = max(cint(output.get("max_group_members")), 0)
    if (
        output.max_group_members
        and output.max_group_members < output.min_group_members
    ):
        frappe.throw(_("Maximum Group Members cannot be less than Minimum Group Members"))
    output.limit = cint(output.get("limit") or 500)
    if output.limit < 1 or output.limit > MAXIMUM_LIMIT:
        frappe.throw(_("Maximum Rows must be between 1 and {0}").format(MAXIMUM_LIMIT))
    return output


def _current_resolution_rows() -> list[dict[str, Any]]:
    memberships = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={"status": ["in", CURRENT_MEMBERSHIP_STATUSES]},
        fields=[
            "name",
            "ccd_master",
            "identity_group",
            "identity_fingerprint",
            "status",
            "originating_decision",
            "valid_from",
        ],
        order_by="valid_from desc, name desc",
        limit_page_length=100_000,
    )
    exclusions = frappe.get_all(
        EXCLUSION_DOCTYPE,
        filters={"status": "Active"},
        fields=[
            "left_record",
            "right_record",
            "left_fingerprint",
            "right_fingerprint",
            "originating_decision",
            "decided_at",
        ],
        order_by="decided_at desc, name desc",
        limit_page_length=100_000,
    )
    candidate_ids = {
        str(row.ccd_master) for row in memberships
    } | {
        str(record_id)
        for exclusion in exclusions
        for record_id in (exclusion.left_record, exclusion.right_record)
    }
    if not candidate_ids:
        return []

    accessible_records = _accessible_record_rows(candidate_ids)
    if not accessible_records:
        return []
    # Exclusion fingerprint validation needs both ends even when only one end
    # is visible to this user. The other record's values never enter the output.
    identity_records = _identity_record_rows(candidate_ids)

    decision_names = {
        str(row.originating_decision) for row in memberships
    } | {
        str(row.originating_decision) for row in exclusions
    }
    decisions = _decision_rows(decision_names)
    policies = {
        name: MatchingPolicy.from_dict(json.loads(row["policy_snapshot_json"]))
        for name, row in decisions.items()
    }
    fingerprint_cache: dict[tuple[str, str], str] = {}

    def current_fingerprint(record_id: str, decision_name: str) -> str:
        key = (record_id, decision_name)
        if key not in fingerprint_cache:
            fingerprint_cache[key] = identity_fingerprint(
                identity_records[record_id], policies[decision_name]
            )
        return fingerprint_cache[key]

    exclusion_counts: Counter[str] = Counter()
    exclusion_decisions: dict[str, list[str]] = defaultdict(list)
    for row in exclusions:
        decision_name = str(row.originating_decision)
        decision = decisions.get(decision_name)
        if not decision or decision["status"] != "Active":
            continue
        left = str(row.left_record)
        right = str(row.right_record)
        if (
            current_fingerprint(left, decision_name) != str(row.left_fingerprint)
            or current_fingerprint(right, decision_name) != str(row.right_fingerprint)
        ):
            continue
        for record_id in (left, right):
            exclusion_counts[record_id] += 1
            if decision_name not in exclusion_decisions[record_id]:
                exclusion_decisions[record_id].append(decision_name)

    group_names = {str(row.identity_group) for row in memberships}
    groups: dict[str, dict[str, Any]] = {}
    ordered_groups = sorted(group_names)
    for offset in range(0, len(ordered_groups), 500):
        group_rows = frappe.get_all(
            GROUP_DOCTYPE,
            filters={"name": ["in", ordered_groups[offset : offset + 500]]},
            fields=["name", "status", "same_source_duplicate_warning"],
            limit_page_length=500,
        )
        groups.update({str(row.name): dict(row) for row in group_rows})
    group_counts = Counter(str(row.identity_group) for row in memberships)
    membership_by_record: dict[str, Any] = {}
    for row in memberships:
        membership_by_record.setdefault(str(row.ccd_master), row)

    output = []
    for record_id in sorted(accessible_records):
        membership = membership_by_record.get(record_id)
        if membership:
            decision_name = str(membership.originating_decision)
            decision = decisions[decision_name]
            group_name = str(membership.identity_group)
            group = groups[group_name]
            fingerprint_changed = (
                current_fingerprint(record_id, decision_name)
                != str(membership.identity_fingerprint)
            )
            identity_state = (
                "Needs Revalidation"
                if fingerprint_changed or membership.status == "Needs Revalidation"
                else "Linked"
            )
            output.append(
                {
                    "ccd_master": record_id,
                    "ccd_reg_source": accessible_records[record_id].get(
                        "ccd_reg_source"
                    )
                    or "",
                    "identity_state": identity_state,
                    "identity_group": group_name,
                    "group_status": group["status"],
                    "active_group_members": group_counts[group_name],
                    "membership_status": membership.status,
                    "active_different_relationships": exclusion_counts[record_id],
                    "decision": decision_name,
                    "decision_origin": decision["origin"],
                    "policy_version": decision["policy_version"],
                    "same_source_warning": cint(
                        group.get("same_source_duplicate_warning")
                    ),
                }
            )
            continue

        decision_names_for_record = exclusion_decisions.get(record_id, [])
        if not decision_names_for_record:
            continue
        decision_name = decision_names_for_record[0]
        decision = decisions[decision_name]
        output.append(
            {
                "ccd_master": record_id,
                "ccd_reg_source": accessible_records[record_id].get(
                    "ccd_reg_source"
                )
                or "",
                "identity_state": "Resolved Separately",
                "identity_group": "",
                "group_status": "",
                "active_group_members": 0,
                "membership_status": "",
                "active_different_relationships": exclusion_counts[record_id],
                "decision": decision_name,
                "decision_origin": decision["origin"],
                "policy_version": decision["policy_version"],
                "same_source_warning": 0,
            }
        )

    state_priority = {"Needs Revalidation": 0, "Linked": 1, "Resolved Separately": 2}
    return sorted(
        output,
        key=lambda row: (
            state_priority[row["identity_state"]],
            row["ccd_reg_source"],
            row["ccd_master"],
        ),
    )


def _accessible_record_rows(record_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    ordered = sorted({str(record_id) for record_id in record_ids})
    for offset in range(0, len(ordered), 500):
        rows = frappe.get_list(
            "CCD Master",
            filters={"name": ["in", ordered[offset : offset + 500]]},
            fields=["name", "ccd_reg_source"],
            limit_page_length=500,
        )
        output.update({str(row.name): dict(row) for row in rows})
    return output


def _identity_record_rows(record_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    ordered = sorted({str(record_id) for record_id in record_ids})
    for offset in range(0, len(ordered), 500):
        rows = frappe.get_all(
            "CCD Master",
            filters={"name": ["in", ordered[offset : offset + 500]]},
            fields=["*"],
            limit_page_length=500,
        )
        for row in rows:
            values = dict(row)
            values["source"] = str(values.get("ccd_reg_source") or "")
            output[str(row.name)] = values
    missing = sorted(set(ordered) - set(output))
    if missing:
        frappe.throw(
            _("CCD Master records no longer exist: {0}").format(", ".join(missing[:5]))
        )
    return output


def _decision_rows(decision_names: Iterable[str]) -> dict[str, dict[str, Any]]:
    ordered = sorted({str(name) for name in decision_names})
    if not ordered:
        return {}
    output: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(ordered), 500):
        rows = frappe.get_all(
            DECISION_DOCTYPE,
            filters={"name": ["in", ordered[offset : offset + 500]]},
            fields=[
                "name",
                "origin",
                "policy_version",
                "policy_snapshot_json",
                "status",
            ],
            limit_page_length=500,
        )
        output.update({str(row.name): dict(row) for row in rows})
    missing = sorted(set(ordered) - set(output))
    if missing:
        frappe.throw(_("Identity Decisions no longer exist: {0}").format(", ".join(missing[:5])))
    return output


def _apply_filters(
    rows: list[dict[str, Any]], filters: frappe._dict
) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if filters.identity_state != "Any Resolved" and row["identity_state"] != filters.identity_state:
            continue
        if filters.get("ccd_master") and row["ccd_master"] != filters.ccd_master:
            continue
        if filters.get("ccd_reg_source") and row["ccd_reg_source"] != filters.ccd_reg_source:
            continue
        if filters.get("identity_group") and row["identity_group"] != filters.identity_group:
            continue
        if filters.get("group_status") and row["group_status"] != filters.group_status:
            continue
        if filters.min_group_members and row["active_group_members"] < filters.min_group_members:
            continue
        if filters.max_group_members and row["active_group_members"] > filters.max_group_members:
            continue
        if filters.get("has_active_different") == "Yes" and not row["active_different_relationships"]:
            continue
        if filters.get("has_active_different") == "No" and row["active_different_relationships"]:
            continue
        output.append(row)
    return output


def _columns() -> list[dict[str, Any]]:
    return [
        {"fieldname": "ccd_master", "label": _("CCD Master"), "fieldtype": "Link", "options": "CCD Master", "width": 145},
        {"fieldname": "ccd_reg_source", "label": _("CCD Registration Source"), "fieldtype": "Data", "width": 185},
        {"fieldname": "identity_state", "label": _("Identity State"), "fieldtype": "Data", "width": 165},
        {"fieldname": "identity_group", "label": _("Identity Group"), "fieldtype": "Link", "options": GROUP_DOCTYPE, "width": 135},
        {"fieldname": "group_status", "label": _("Group Status"), "fieldtype": "Data", "width": 145},
        {"fieldname": "active_group_members", "label": _("Active Group Members"), "fieldtype": "Int", "width": 165},
        {"fieldname": "membership_status", "label": _("Membership Status"), "fieldtype": "Data", "width": 155},
        {"fieldname": "active_different_relationships", "label": _("Active Different Relationships"), "fieldtype": "Int", "width": 205},
        {"fieldname": "decision_origin", "label": _("Decision Origin"), "fieldtype": "Data", "width": 170},
        {"fieldname": "decision", "label": _("Current Decision"), "fieldtype": "Link", "options": DECISION_DOCTYPE, "width": 135},
        {"fieldname": "policy_version", "label": _("Policy / Model Version"), "fieldtype": "Data", "width": 170},
        {"fieldname": "same_source_warning", "label": _("Same-source Warning"), "fieldtype": "Check", "width": 155},
    ]


def _report_summary(
    displayed_rows: list[dict[str, Any]], matching_count: int
) -> list[dict[str, Any]]:
    counts = Counter(row["identity_state"] for row in displayed_rows)
    return [
        {"value": matching_count, "label": _("Matching CCD Masters"), "datatype": "Int"},
        {"value": len(displayed_rows), "label": _("Displayed"), "datatype": "Int"},
        {"value": counts["Linked"], "label": _("Linked displayed"), "datatype": "Int", "indicator": "Green"},
        {"value": counts["Needs Revalidation"], "label": _("Need revalidation displayed"), "datatype": "Int", "indicator": "Orange"},
        {"value": counts["Resolved Separately"], "label": _("Separate displayed"), "datatype": "Int", "indicator": "Blue"},
    ]
