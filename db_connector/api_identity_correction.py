"""Audited replacement-partition corrections for complete identity components."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

import frappe

from db_connector.api_identity_resolution import (
    CURRENT_MEMBERSHIP_STATUSES,
    DECISION_DOCTYPE,
    EXCLUSION_DOCTYPE,
    GROUP_DOCTYPE,
    MEMBERSHIP_DOCTYPE,
    _append_event,
    _lock_records,
    _policy,
    _record_rows,
    materialization_enabled,
    materialize_identity,
)
from db_connector.fuzzy_matching.correction import (
    MAX_CORRECTION_RECORDS,
    correction_key,
    exclusions_for_partition,
    normalize_partition,
    stable_payload_fingerprint,
)
from db_connector.fuzzy_matching.identity import complete_hkid_conflicts, identity_fingerprint


CORRECTION_DOCTYPE = "CCD Identity Correction"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
ACTIVATION_ITEM_DOCTYPE = "CCD Identity Activation Item"
COMPONENT_REVIEW_DOCTYPE = "CCD Match Component Review"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
SUPPORTED_SOURCE_ORIGINS = {
    "Tiered Evidence",
    "Component Review",
    "Splink Human Review",
    "Governance Override",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _as_bool(value: Any) -> bool:
    return str(value or "0").strip().casefold() in {"1", "true", "yes", "on"}


def _load_list(value: Any, label: str) -> list[Any]:
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw(f"{label} must be valid JSON")
    if not isinstance(loaded, (list, tuple)):
        frappe.throw(f"{label} must be a JSON list")
    return list(loaded)


def _lock_named_rows(doctype: str, names: Iterable[str]) -> None:
    ordered = tuple(sorted({str(item) for item in names if str(item)}))
    if not ordered:
        return
    placeholders = ", ".join(["%s"] * len(ordered))
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` "
        f"WHERE name IN ({placeholders}) ORDER BY name FOR UPDATE",
        ordered,
    )


def _rows_as_dicts(rows: Iterable[Any], fields: Iterable[str]) -> list[dict[str, Any]]:
    return [
        {field: row.get(field) for field in fields}
        for row in sorted(rows, key=lambda item: str(item.name))
    ]


def _decision_participants(decision: Any) -> set[str]:
    values = _load_list(decision.participant_records_json or "[]", "Decision participants")
    records = {str(item).strip() for item in values if str(item).strip()}
    if len(records) < 2:
        frappe.throw("The source Identity Decision has an incomplete participant scope")
    return records


def _current_memberships_for_records(record_ids: Iterable[str]) -> list[Any]:
    ids = tuple(sorted({str(item) for item in record_ids}))
    if not ids:
        return []
    return frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "ccd_master": ["in", ids],
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=[
            "name",
            "ccd_master",
            "identity_group",
            "identity_fingerprint",
            "status",
            "originating_decision",
        ],
        order_by="name",
        limit_page_length=100_000,
    )


def _memberships_for_groups(group_names: Iterable[str]) -> list[Any]:
    names = tuple(sorted({str(item) for item in group_names}))
    if not names:
        return []
    return frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "identity_group": ["in", names],
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=[
            "name",
            "ccd_master",
            "identity_group",
            "identity_fingerprint",
            "status",
            "originating_decision",
        ],
        order_by="name",
        limit_page_length=100_000,
    )


def _expand_complete_scope(seed_records: Iterable[str]) -> tuple[set[str], list[Any]]:
    """Expand through every current Same group so no correction can split it accidentally."""
    records = {str(item) for item in seed_records}
    memberships: dict[str, Any] = {}
    while True:
        rows = _current_memberships_for_records(records)
        group_names = {str(row.identity_group) for row in rows}
        group_rows = _memberships_for_groups(group_names)
        for row in (*rows, *group_rows):
            memberships[str(row.name)] = row
        expanded_records = records | {
            str(row.ccd_master) for row in memberships.values()
        }
        if expanded_records == records:
            break
        records = expanded_records
        if len(records) > MAX_CORRECTION_RECORDS:
            frappe.throw(
                f"The complete live identity scope exceeds the bounded {MAX_CORRECTION_RECORDS}-record correction limit"
            )

    counts = Counter(str(row.ccd_master) for row in memberships.values())
    duplicates = sorted(record_id for record_id, count in counts.items() if count > 1)
    if duplicates:
        frappe.throw(
            "The live identity state has multiple current Memberships for: "
            + ", ".join(duplicates)
        )
    return records, sorted(memberships.values(), key=lambda row: str(row.name))


def _active_exclusions(record_ids: Iterable[str]) -> list[Any]:
    ids = tuple(sorted({str(item) for item in record_ids}))
    if len(ids) < 2:
        return []
    return frappe.get_all(
        EXCLUSION_DOCTYPE,
        filters={
            "left_record": ["in", ids],
            "right_record": ["in", ids],
            "status": "Active",
        },
        fields=[
            "name",
            "left_record",
            "right_record",
            "left_fingerprint",
            "right_fingerprint",
            "originating_decision",
            "status",
        ],
        order_by="name",
        limit_page_length=100_000,
    )


def _current_partition(
    record_ids: Iterable[str], memberships: Iterable[Any]
) -> tuple[tuple[str, ...], ...]:
    by_group: dict[str, list[str]] = {}
    grouped: set[str] = set()
    for row in memberships:
        by_group.setdefault(str(row.identity_group), []).append(str(row.ccd_master))
        grouped.add(str(row.ccd_master))
    groups = [tuple(sorted(members)) for members in by_group.values()]
    groups.extend((record_id,) for record_id in sorted(set(record_ids) - grouped))
    return tuple(sorted(groups))


def _complete_context(source_decision: str) -> dict[str, Any]:
    decision_name = str(source_decision or "").strip()
    if not decision_name:
        frappe.throw("Select an Identity Decision to correct")
    decision = frappe.get_doc(DECISION_DOCTYPE, decision_name)
    if decision.status != "Active":
        frappe.throw("Only a current Active Identity Decision can start a correction")
    if decision.origin not in SUPPORTED_SOURCE_ORIGINS:
        frappe.throw("This Identity Decision origin is not supported by complete correction")
    if not decision.policy_snapshot_json:
        frappe.throw("The source Identity Decision has no reproducible policy snapshot")

    record_ids, memberships = _expand_complete_scope(_decision_participants(decision))
    try:
        normalize_partition(record_ids, [(record_id,) for record_id in record_ids])
    except ValueError as exc:
        frappe.throw(str(exc))

    group_names = sorted({str(row.identity_group) for row in memberships})
    groups = frappe.get_all(
        GROUP_DOCTYPE,
        filters={"name": ["in", group_names]},
        fields=[
            "name",
            "status",
            "originating_decision",
            "group_fingerprint",
            "active_member_count",
        ],
        order_by="name",
        limit_page_length=max(len(group_names), 1),
    ) if group_names else []
    if len(groups) != len(group_names):
        frappe.throw("The live identity scope references a missing Identity Group")

    exclusions = _active_exclusions(record_ids)
    decision_names = {
        decision.name,
        *(str(row.originating_decision) for row in memberships),
        *(str(row.originating_decision) for row in groups),
        *(str(row.originating_decision) for row in exclusions),
    }
    decisions = frappe.get_all(
        DECISION_DOCTYPE,
        filters={"name": ["in", sorted(decision_names)]},
        fields=[
            "name",
            "status",
            "decision_type",
            "decision_version",
            "origin",
            "origin_doctype",
            "origin_document",
        ],
        order_by="name",
        limit_page_length=max(len(decision_names), 1),
    )
    missing_decisions = sorted(decision_names - {str(row.name) for row in decisions})
    if missing_decisions:
        frappe.throw("The live identity scope references missing Decisions: " + ", ".join(missing_decisions))

    record_rows = _record_rows(record_ids)
    policy = _policy(decision.policy_snapshot_json)
    fingerprints = {
        record_id: identity_fingerprint(row, policy)
        for record_id, row in record_rows.items()
    }
    modified = {
        record_id: str(row.get("modified") or "")
        for record_id, row in record_rows.items()
    }
    membership_fields = (
        "name",
        "ccd_master",
        "identity_group",
        "identity_fingerprint",
        "status",
        "originating_decision",
    )
    group_fields = (
        "name",
        "status",
        "originating_decision",
        "group_fingerprint",
        "active_member_count",
    )
    exclusion_fields = (
        "name",
        "left_record",
        "right_record",
        "left_fingerprint",
        "right_fingerprint",
        "originating_decision",
        "status",
    )
    decision_fields = (
        "name",
        "status",
        "decision_type",
        "decision_version",
        "origin",
        "origin_doctype",
        "origin_document",
    )
    scope_payload = {
        "source_decision": {
            "name": decision.name,
            "status": decision.status,
            "origin": decision.origin,
            "origin_doctype": decision.origin_doctype,
            "origin_document": decision.origin_document,
        },
        "records": [
            {
                "name": record_id,
                "modified": modified[record_id],
                "identity_fingerprint": fingerprints[record_id],
            }
            for record_id in sorted(record_ids)
        ],
        "memberships": _rows_as_dicts(memberships, membership_fields),
        "groups": _rows_as_dicts(groups, group_fields),
        "exclusions": _rows_as_dicts(exclusions, exclusion_fields),
        "decisions": _rows_as_dicts(decisions, decision_fields),
    }
    return {
        "source": decision,
        "record_ids": tuple(sorted(record_ids)),
        "record_rows": record_rows,
        "fingerprints": fingerprints,
        "modified": modified,
        "memberships": memberships,
        "groups": groups,
        "exclusions": exclusions,
        "decisions": decisions,
        "current_groups": _current_partition(record_ids, memberships),
        "scope_fingerprint": stable_payload_fingerprint(scope_payload),
        "record_summaries": [
            {
                "record_id": record_id,
                "source": str(
                    record_rows[record_id].get("ccd_reg_source")
                    or record_rows[record_id].get("source")
                    or ""
                ),
            }
            for record_id in sorted(record_ids)
        ],
    }


def _decision_type(groups: tuple[tuple[str, ...], ...]) -> str:
    if len(groups) == 1:
        return "Same"
    if all(len(group) == 1 for group in groups):
        return "Different"
    return "Partition"


def _preview(source_decision: str, replacement_groups_json: Any) -> dict[str, Any]:
    context = _complete_context(source_decision)
    raw_groups = _load_list(replacement_groups_json, "Replacement groups")
    try:
        replacement_groups = normalize_partition(context["record_ids"], raw_groups)
    except ValueError as exc:
        frappe.throw(str(exc))
    replacement_exclusions = exclusions_for_partition(replacement_groups)
    current_groups = context["current_groups"]

    records = context["record_rows"]
    for row in records.values():
        row["source"] = str(row.get("ccd_reg_source") or row.get("source") or "")
    policy = _policy(context["source"].policy_snapshot_json)
    hkid_conflicts = complete_hkid_conflicts(replacement_groups, records, policy)
    same_source_groups = []
    for group in replacement_groups:
        source_counts = Counter(str(records[item].get("source") or "") for item in group)
        if any(count > 1 for source, count in source_counts.items() if source):
            same_source_groups.append(group)

    warnings: list[str] = []
    if hkid_conflicts:
        warnings.append("complete_hkid_conflict_governance_override")
    if same_source_groups:
        warnings.append("same_source_duplicates_governance_override")
    if any(str(row.status) != "Active" for row in context["memberships"]):
        warnings.append("current_membership_needs_revalidation")
    if any(str(row.status) != "Active" for row in context["groups"]):
        warnings.append("current_group_needs_revalidation")
    if any(str(row.status) != "Active" for row in context["decisions"]):
        warnings.append("scope_contains_already_superseded_decision_provenance")

    key = correction_key(
        context["source"].name,
        context["scope_fingerprint"],
        replacement_groups,
    )
    return {
        "zero_write": True,
        "eligible": True,
        "source_identity_decision": context["source"].name,
        "source_origin": context["source"].origin,
        "source_origin_doctype": context["source"].origin_doctype,
        "source_origin_document": context["source"].origin_document,
        "materialization_enabled": materialization_enabled(automated=False),
        "requires_materialization_disabled": True,
        "scope_fingerprint": context["scope_fingerprint"],
        "correction_key": key,
        "records": context["record_summaries"],
        "current_groups": current_groups,
        "replacement_groups": replacement_groups,
        "replacement_exclusions": replacement_exclusions,
        "changed": replacement_groups != current_groups,
        "warnings": warnings,
        "requires_safety_confirmation": bool(warnings),
        "planned": {
            "replacement_decision_type": _decision_type(replacement_groups),
            "ended_groups": len(context["groups"]),
            "ended_memberships": len(context["memberships"]),
            "superseded_decisions": sum(
                1 for row in context["decisions"] if str(row.status) == "Active"
            ),
            "superseded_exclusions": len(context["exclusions"]),
            "new_groups": sum(1 for group in replacement_groups if len(group) > 1),
            "new_memberships": sum(len(group) for group in replacement_groups if len(group) > 1),
            "new_exclusions": len(replacement_exclusions),
            "physical_ccd_master_merges": 0,
        },
        "_context": context,
    }


def _public_preview(preview: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in preview.items() if key != "_context"}


@frappe.whitelist()
def get_complete_component_correction_context(source_decision: str) -> dict[str, Any]:
    """Return the current complete scope without proposing or writing a change."""
    _require_manager()
    context = _complete_context(source_decision)
    return {
        "zero_write": True,
        "eligible": True,
        "source_identity_decision": context["source"].name,
        "source_origin": context["source"].origin,
        "source_origin_doctype": context["source"].origin_doctype,
        "source_origin_document": context["source"].origin_document,
        "materialization_enabled": materialization_enabled(automated=False),
        "scope_fingerprint": context["scope_fingerprint"],
        "records": context["record_summaries"],
        "current_groups": context["current_groups"],
        "current_group_count": len(context["groups"]),
        "current_membership_count": len(context["memberships"]),
        "current_exclusion_count": len(context["exclusions"]),
        "current_decision_count": len(context["decisions"]),
        "maximum_records": MAX_CORRECTION_RECORDS,
    }


@frappe.whitelist()
def preview_complete_component_correction(
    source_decision: str,
    replacement_groups_json: Any,
) -> dict[str, Any]:
    """Zero-write preview of an exact replacement identity partition."""
    _require_manager()
    return _public_preview(_preview(source_decision, replacement_groups_json))


def _set_existing_fields(doctype: str, name: str, values: dict[str, Any]) -> None:
    fieldnames = {field.fieldname for field in frappe.get_meta(doctype).fields}
    safe_values = {key: value for key, value in values.items() if key in fieldnames}
    if safe_values:
        frappe.db.set_value(doctype, name, safe_values, update_modified=False)


def _mark_origin_corrected(
    decisions: Iterable[Any],
    correction_name: str,
    replacement_decision: str,
    reason: str,
    now: Any,
) -> None:
    affected_canaries: set[str] = set()
    for decision in decisions:
        if str(decision.status) != "Active":
            continue
        origin = str(decision.origin)
        origin_document = str(decision.origin_document)
        if origin == "Tiered Evidence":
            recommendations = frappe.get_all(
                RECOMMENDATION_DOCTYPE,
                filters={"identity_decision": decision.name},
                fields=["name", "canary_run"],
                limit_page_length=100_000,
            )
            for row in recommendations:
                _set_existing_fields(
                    RECOMMENDATION_DOCTYPE,
                    row.name,
                    {
                        "status": "Superseded",
                        "ended_at": now,
                        "ended_by": frappe.session.user,
                        "end_reason": reason,
                    },
                )
                affected_canaries.add(str(row.canary_run))
            for item_name in frappe.get_all(
                ACTIVATION_ITEM_DOCTYPE,
                filters={"identity_decision": decision.name},
                pluck="name",
                limit_page_length=100_000,
            ):
                _set_existing_fields(
                    ACTIVATION_ITEM_DOCTYPE,
                    item_name,
                    {"status": "Corrected", "error_code": f"corrected_by:{correction_name}"},
                )
        elif origin == "Component Review" and frappe.db.exists(
            COMPONENT_REVIEW_DOCTYPE, origin_document
        ):
            _set_existing_fields(
                COMPONENT_REVIEW_DOCTYPE,
                origin_document,
                {
                    "materialization_status": "Corrected",
                    "correction_decision": replacement_decision,
                    "corrected_at": now,
                    "corrected_by": frappe.session.user,
                    "correction_reason": reason,
                    "materialization_error": None,
                },
            )
        elif origin == "Splink Human Review" and frappe.db.exists(
            CANDIDATE_DOCTYPE, origin_document
        ):
            _set_existing_fields(
                CANDIDATE_DOCTYPE,
                origin_document,
                {
                    "materialization_status": "Reversed",
                    "correction_decision": replacement_decision,
                    "reversed_at": now,
                    "reversed_by": frappe.session.user,
                    "reversal_reason": reason,
                    "materialization_error": None,
                },
            )
        elif origin == "Governance Override" and decision.origin_doctype == CORRECTION_DOCTYPE:
            if frappe.db.exists(CORRECTION_DOCTYPE, origin_document):
                _set_existing_fields(
                    CORRECTION_DOCTYPE,
                    origin_document,
                    {"status": "Superseded", "superseded_by": correction_name},
                )
        elif origin == "Governance Override" and decision.origin_doctype == "CCD Identity Overlap Resolution":
            if frappe.db.exists(decision.origin_doctype, origin_document):
                _set_existing_fields(
                    decision.origin_doctype,
                    origin_document,
                    {
                        "status": "Superseded",
                        "superseded_by_identity_decision": replacement_decision,
                    },
                )

    if affected_canaries:
        from db_connector.api_fuzzy_canary import _refresh_run_counts

        for canary_name in sorted(affected_canaries):
            _refresh_run_counts(canary_name)


def _link_replacement_groups(
    old_groups: Iterable[Any],
    old_memberships: Iterable[Any],
    replacement_decision: str,
) -> None:
    new_groups = frappe.get_all(
        GROUP_DOCTYPE,
        filters={"originating_decision": replacement_decision},
        fields=["name"],
        limit_page_length=100_000,
    )
    new_memberships = _memberships_for_groups([row.name for row in new_groups])
    old_members_by_group: dict[str, set[str]] = {}
    new_members_by_group: dict[str, set[str]] = {}
    for row in old_memberships:
        old_members_by_group.setdefault(str(row.identity_group), set()).add(str(row.ccd_master))
    for row in new_memberships:
        new_members_by_group.setdefault(str(row.identity_group), set()).add(str(row.ccd_master))

    for old_group in old_groups:
        old_members = old_members_by_group.get(str(old_group.name), set())
        overlapping_new = [
            group_name
            for group_name, members in new_members_by_group.items()
            if old_members.intersection(members)
        ]
        if len(overlapping_new) == 1:
            _set_existing_fields(
                GROUP_DOCTYPE,
                old_group.name,
                {"superseded_by": overlapping_new[0]},
            )
    for new_group in new_groups:
        new_members = new_members_by_group.get(str(new_group.name), set())
        overlapping_old = [
            group_name
            for group_name, members in old_members_by_group.items()
            if new_members.intersection(members)
        ]
        if len(overlapping_old) == 1:
            _set_existing_fields(
                GROUP_DOCTYPE,
                new_group.name,
                {"supersedes": overlapping_old[0]},
            )


@frappe.whitelist()
def apply_complete_component_correction(
    source_decision: str,
    replacement_groups_json: Any,
    expected_scope_fingerprint: str,
    reason: str,
    confirm_source_decision: str,
    confirm_safety_warnings: int | str = 0,
    is_demonstration: int | str = 0,
) -> dict[str, Any]:
    """Atomically replace every current relationship in one complete bounded scope."""
    _require_manager()
    source_decision = str(source_decision or "").strip()
    expected_scope_fingerprint = str(expected_scope_fingerprint or "").strip()
    reason = str(reason or "").strip()
    if not source_decision or str(confirm_source_decision or "").strip() != source_decision:
        frappe.throw("Type the exact source Identity Decision ID to confirm this correction")
    if not expected_scope_fingerprint:
        frappe.throw("The zero-write correction preview is required")
    if not reason:
        frappe.throw("A correction reason is required")
    if materialization_enabled(automated=False):
        frappe.throw("Disable Materialization before applying a complete identity correction")
    demonstration = _as_bool(is_demonstration)

    raw_groups = _load_list(replacement_groups_json, "Replacement groups")
    if any(not isinstance(group, (list, tuple)) for group in raw_groups):
        frappe.throw("Each replacement group must be a JSON list of CCD record IDs")
    flat_records = [str(item) for group in raw_groups for item in group]
    try:
        submitted_groups = normalize_partition(flat_records, raw_groups)
    except ValueError as exc:
        frappe.throw(str(exc))
    submitted_key = correction_key(
        source_decision,
        expected_scope_fingerprint,
        submitted_groups,
    )
    existing = frappe.db.get_value(
        CORRECTION_DOCTYPE,
        {"correction_key": submitted_key, "status": "Applied"},
        ["name", "replacement_identity_decision"],
        as_dict=True,
    )
    if existing:
        return {
            "status": "Already Applied",
            "correction": existing.name,
            "replacement_identity_decision": existing.replacement_identity_decision,
        }

    try:
        preview = _preview(source_decision, submitted_groups)
        context = preview["_context"]
        if preview["scope_fingerprint"] != expected_scope_fingerprint:
            frappe.throw("The complete identity scope changed after preview; preview it again")
        if preview["correction_key"] != submitted_key:
            frappe.throw("The submitted replacement partition does not match its frozen preview")
        if not preview["changed"]:
            frappe.throw("The replacement partition is identical to the current identity state")
        if preview["requires_safety_confirmation"] and not _as_bool(confirm_safety_warnings):
            frappe.throw("Explicitly confirm every safety warning shown by the preview")

        _lock_records(context["record_ids"])
        _lock_named_rows(DECISION_DOCTYPE, [row.name for row in context["decisions"]])
        _lock_named_rows(GROUP_DOCTYPE, [row.name for row in context["groups"]])
        _lock_named_rows(MEMBERSHIP_DOCTYPE, [row.name for row in context["memberships"]])
        _lock_named_rows(EXCLUSION_DOCTYPE, [row.name for row in context["exclusions"]])
        locked_preview = _preview(source_decision, submitted_groups)
        if (
            locked_preview["scope_fingerprint"] != expected_scope_fingerprint
            or locked_preview["correction_key"] != submitted_key
        ):
            frappe.throw("The complete identity scope changed while it was being locked; preview it again")
        context = locked_preview["_context"]

        now = frappe.utils.now_datetime()
        correction = frappe.get_doc(
            {
                "doctype": CORRECTION_DOCTYPE,
                "correction_key": submitted_key,
                "status": "Applying",
                "source_identity_decision": source_decision,
                "source_origin": context["source"].origin,
                "source_origin_doctype": context["source"].origin_doctype,
                "source_origin_document": context["source"].origin_document,
                "scope_fingerprint": expected_scope_fingerprint,
                "participant_records_json": _json(context["record_ids"]),
                "prior_identity_decisions_json": _json([row.name for row in context["decisions"]]),
                "prior_identity_groups_json": _json([row.name for row in context["groups"]]),
                "prior_memberships_json": _json(
                    _rows_as_dicts(
                        context["memberships"],
                        (
                            "name",
                            "ccd_master",
                            "identity_group",
                            "identity_fingerprint",
                            "status",
                            "originating_decision",
                        ),
                    )
                ),
                "prior_exclusions_json": _json(
                    _rows_as_dicts(
                        context["exclusions"],
                        (
                            "name",
                            "left_record",
                            "right_record",
                            "left_fingerprint",
                            "right_fingerprint",
                            "originating_decision",
                            "status",
                        ),
                    )
                ),
                "replacement_groups_json": _json(locked_preview["replacement_groups"]),
                "replacement_exclusions_json": _json(locked_preview["replacement_exclusions"]),
                "correction_reason": reason,
                "warnings_json": _json(locked_preview["warnings"]),
                "is_demonstration": int(demonstration),
                "applied_at": now,
                "applied_by": frappe.session.user,
                "ended_group_count": len(context["groups"]),
                "ended_membership_count": len(context["memberships"]),
                "superseded_exclusion_count": len(context["exclusions"]),
            }
        ).insert(ignore_permissions=True)
        event_nonce = f"complete_identity_correction:{correction.name}:{now}"

        for membership in context["memberships"]:
            frappe.db.set_value(
                MEMBERSHIP_DOCTYPE,
                membership.name,
                {
                    "status": "Ended",
                    "valid_to": now,
                    "ended_reason": reason,
                    "ended_by": frappe.session.user,
                },
                update_modified=False,
            )
            _append_event(
                entity_doctype=MEMBERSHIP_DOCTYPE,
                entity_name=membership.name,
                event_type="End",
                reason=reason,
                nonce=event_nonce,
                from_status=str(membership.status),
                to_status="Ended",
                identity_decision=str(membership.originating_decision),
                identity_group=str(membership.identity_group),
                identity_membership=membership.name,
                metadata={"identity_correction": correction.name},
                is_demonstration=demonstration,
            )
        for group in context["groups"]:
            old_members = {
                str(row.ccd_master)
                for row in context["memberships"]
                if str(row.identity_group) == str(group.name)
            }
            replacement_overlap_count = sum(
                1
                for replacement_group in locked_preview["replacement_groups"]
                if old_members.intersection(replacement_group)
            )
            frappe.db.set_value(
                GROUP_DOCTYPE,
                group.name,
                {"status": "Ended", "active_member_count": 0, "last_validation_at": now},
                update_modified=False,
            )
            _append_event(
                entity_doctype=GROUP_DOCTYPE,
                entity_name=group.name,
                event_type="Split" if replacement_overlap_count > 1 else "End",
                reason=reason,
                nonce=event_nonce,
                from_status=str(group.status),
                to_status="Ended",
                identity_decision=str(group.originating_decision),
                identity_group=group.name,
                metadata={
                    "identity_correction": correction.name,
                    "replacement_groups": locked_preview["replacement_groups"],
                },
                is_demonstration=demonstration,
            )
        for exclusion in context["exclusions"]:
            frappe.db.set_value(
                EXCLUSION_DOCTYPE,
                exclusion.name,
                {"status": "Superseded"},
                update_modified=False,
            )
            _append_event(
                entity_doctype=EXCLUSION_DOCTYPE,
                entity_name=exclusion.name,
                event_type="Supersede",
                reason=reason,
                nonce=event_nonce,
                from_status="Active",
                to_status="Superseded",
                identity_decision=str(exclusion.originating_decision),
                metadata={"identity_correction": correction.name},
                is_demonstration=demonstration,
            )

        result = materialize_identity(
            origin="Governance Override",
            origin_doctype=CORRECTION_DOCTYPE,
            origin_document=correction.name,
            policy_snapshot_json=context["source"].policy_snapshot_json,
            policy_snapshot_sha256=context["source"].policy_snapshot_sha256,
            matching_policy=context["source"].matching_policy,
            record_ids=context["record_ids"],
            groups=locked_preview["replacement_groups"],
            exclusions=locked_preview["replacement_exclusions"],
            expected_fingerprints=context["fingerprints"],
            expected_modified=context["modified"],
            reason_codes=["manager_complete_component_correction"],
            review_context={
                "identity_correction": correction.name,
                "source_identity_decision": source_decision,
                "superseded_identity_decisions": [row.name for row in context["decisions"]],
                "correction_reason": reason,
                "accepted_warnings": locked_preview["warnings"],
            },
            governance_override=True,
            governance_notes=reason,
            is_demonstration=demonstration,
            require_enabled=False,
        )
        replacement_decision = str(result["identity_decision"])
        active_prior_decisions = [
            row for row in context["decisions"] if str(row.status) == "Active"
        ]
        max_version = max(int(row.decision_version or 1) for row in context["decisions"])
        frappe.db.set_value(
            DECISION_DOCTYPE,
            replacement_decision,
            {"decision_version": max_version + 1, "supersedes": source_decision},
            update_modified=False,
        )
        for decision in active_prior_decisions:
            frappe.db.set_value(
                DECISION_DOCTYPE,
                decision.name,
                {"status": "Superseded", "superseded_by": replacement_decision},
                update_modified=False,
            )
            _append_event(
                entity_doctype=DECISION_DOCTYPE,
                entity_name=decision.name,
                event_type="Supersede",
                reason=reason,
                nonce=event_nonce,
                from_status="Active",
                to_status="Superseded",
                identity_decision=replacement_decision,
                metadata={"identity_correction": correction.name},
                is_demonstration=demonstration,
            )

        new_exclusions = frappe.get_all(
            EXCLUSION_DOCTYPE,
            filters={"originating_decision": replacement_decision},
            fields=["name", "left_record", "right_record"],
            limit_page_length=100_000,
        )
        new_exclusion_by_pair = {
            tuple(sorted((str(row.left_record), str(row.right_record)))): str(row.name)
            for row in new_exclusions
        }
        for old_exclusion in context["exclusions"]:
            replacement = new_exclusion_by_pair.get(
                tuple(sorted((str(old_exclusion.left_record), str(old_exclusion.right_record))))
            )
            if replacement:
                _set_existing_fields(
                    EXCLUSION_DOCTYPE,
                    old_exclusion.name,
                    {"superseded_by": replacement},
                )

        _link_replacement_groups(
            context["groups"],
            context["memberships"],
            replacement_decision,
        )
        _mark_origin_corrected(
            active_prior_decisions,
            correction.name,
            replacement_decision,
            reason,
            now,
        )
        frappe.db.set_value(
            CORRECTION_DOCTYPE,
            correction.name,
            {
                "status": "Applied",
                "replacement_identity_decision": replacement_decision,
                "created_group_count": int(result.get("created_groups") or 0),
                "created_membership_count": int(result.get("created_memberships") or 0),
                "created_exclusion_count": int(result.get("created_exclusions") or 0),
            },
            update_modified=False,
        )
        _append_event(
            entity_doctype=CORRECTION_DOCTYPE,
            entity_name=correction.name,
            event_type="Activate",
            reason=reason,
            nonce=event_nonce,
            from_status="Applying",
            to_status="Applied",
            identity_decision=replacement_decision,
            metadata={
                "source_identity_decision": source_decision,
                "replacement_groups": locked_preview["replacement_groups"],
            },
            is_demonstration=demonstration,
        )
        frappe.db.commit()
        return {
            "status": "Applied",
            "correction": correction.name,
            "source_identity_decision": source_decision,
            "replacement_identity_decision": replacement_decision,
            "replacement_decision_type": locked_preview["planned"]["replacement_decision_type"],
            "ended_groups": len(context["groups"]),
            "ended_memberships": len(context["memberships"]),
            "superseded_exclusions": len(context["exclusions"]),
            "created_groups": int(result.get("created_groups") or 0),
            "created_memberships": int(result.get("created_memberships") or 0),
            "created_exclusions": int(result.get("created_exclusions") or 0),
            "is_demonstration": demonstration,
        }
    except Exception:
        frappe.db.rollback()
        raise
