"""Governed, reversible CCD identity links.

This module is the only write path for Identity Decisions, Groups,
Memberships, Exclusions, and lifecycle Events.  It never merges or updates a
CCD Master document and is protected by a default-off site setting.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Iterable

import frappe

from db_connector.api_fuzzy_evaluation import REVIEW_ROLE, SENSITIVE_ROLE
from db_connector.fuzzy_matching.identity import (
    MATERIALIZER_VERSION,
    build_materialization_plan,
    complete_hkid_conflicts,
    fingerprint_scoped_exclusion_conflicts,
    identity_fingerprint,
)
from db_connector.fuzzy_matching.policy import MatchingPolicy

DECISION_DOCTYPE = "CCD Identity Decision"
GROUP_DOCTYPE = "CCD Identity Group"
MEMBERSHIP_DOCTYPE = "CCD Identity Membership"
EXCLUSION_DOCTYPE = "CCD Identity Exclusion"
EVENT_DOCTYPE = "CCD Identity Event"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
CURRENT_MEMBERSHIP_STATUSES = ("Active", "Needs Revalidation")
HUMAN_ORIGINS = {"Splink Human Review", "Component Review", "Governance Override"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_identity_reader(ccd_master_name: str) -> None:
    roles = set(frappe.get_roles())
    if not ({"System Manager", REVIEW_ROLE, SENSITIVE_ROLE} & roles):
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)
    if not frappe.has_permission("CCD Master", "read", doc=ccd_master_name):
        frappe.throw("You cannot read this CCD Master", frappe.PermissionError)


def _has_sensitive_access() -> bool:
    roles = set(frappe.get_roles())
    return "System Manager" in roles or SENSITIVE_ROLE in roles


def _settings() -> Any:
    return frappe.get_single(SETTINGS_DOCTYPE)


def materialization_enabled(*, automated: bool = True) -> bool:
    settings = _settings()
    return bool(settings.materialization_enabled) and (
        not automated or not bool(settings.automation_paused)
    )


def _policy(snapshot_json: str | dict[str, Any]) -> MatchingPolicy:
    value = json.loads(snapshot_json) if isinstance(snapshot_json, str) else snapshot_json
    return MatchingPolicy.from_dict(value)


def _record_rows(record_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ordered = tuple(sorted({str(item) for item in record_ids if str(item)}))
    if not ordered:
        return {}
    rows = frappe.get_all(
        "CCD Master",
        filters={"name": ["in", ordered]},
        fields=["*"],
        limit_page_length=max(len(ordered), 1),
    )
    output = {str(row.name): dict(row) for row in rows}
    missing = sorted(set(ordered) - set(output))
    if missing:
        frappe.throw(f"CCD Master records no longer exist: {', '.join(missing[:5])}")
    return output


def _lock_records(record_ids: Iterable[str]) -> None:
    ordered = tuple(sorted({str(item) for item in record_ids if str(item)}))
    if not ordered:
        return
    placeholders = ", ".join(["%s"] * len(ordered))
    frappe.db.sql(
        f"SELECT name FROM `tabCCD Master` WHERE name IN ({placeholders}) ORDER BY name FOR UPDATE",
        ordered,
    )


def _event_key(entity_doctype: str, entity_name: str, event_type: str, nonce: str) -> str:
    return hashlib.sha256(
        f"{entity_doctype}\x1f{entity_name}\x1f{event_type}\x1f{nonce}".encode()
    ).hexdigest()


def _append_event(
    *,
    entity_doctype: str,
    entity_name: str,
    event_type: str,
    reason: str,
    nonce: str,
    from_status: str = "",
    to_status: str = "",
    identity_decision: str = "",
    identity_group: str = "",
    identity_membership: str = "",
    metadata: dict[str, Any] | None = None,
    is_demonstration: bool = False,
) -> str:
    key = _event_key(entity_doctype, entity_name, event_type, nonce)
    existing = frappe.db.get_value(EVENT_DOCTYPE, {"event_key": key}, "name")
    if existing:
        return str(existing)
    event = frappe.get_doc(
        {
            "doctype": EVENT_DOCTYPE,
            "event_key": key,
            "entity_doctype": entity_doctype,
            "entity_name": entity_name,
            "identity_decision": identity_decision or None,
            "identity_group": identity_group or None,
            "identity_membership": identity_membership or None,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "event_at": frappe.utils.now_datetime(),
            "actor": frappe.session.user,
            "is_demonstration": int(is_demonstration),
            "metadata_json": _json(metadata or {}),
        }
    ).insert(ignore_permissions=True)
    return event.name


def _current_memberships(record_ids: Iterable[str]) -> list[Any]:
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
        ],
        limit_page_length=max(len(ids) * 2, 1),
    )


def _active_group_members(group_name: str) -> set[str]:
    return {
        str(value)
        for value in frappe.get_all(
            MEMBERSHIP_DOCTYPE,
            filters={
                "identity_group": group_name,
                "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
            },
            pluck="ccd_master",
            limit_page_length=100_000,
        )
    }


def _existing_group_conflicts(
    groups: tuple[tuple[str, ...], ...], memberships: list[Any]
) -> tuple[str, ...]:
    by_record = {str(row.ccd_master): row for row in memberships}
    conflicts: set[str] = set()
    for group in groups:
        desired = set(group)
        existing_groups = {
            str(by_record[record_id].identity_group)
            for record_id in desired
            if record_id in by_record
        }
        if len(existing_groups) > 1:
            conflicts.add("conflicting_active_identity_groups")
            continue
        if existing_groups:
            existing_group = next(iter(existing_groups))
            if not _active_group_members(existing_group).issubset(desired):
                conflicts.add("partial_existing_identity_group")
    return tuple(sorted(conflicts))


def _active_exclusion_rows(record_ids: Iterable[str]) -> list[tuple[str, str, str, str]]:
    ids = tuple(sorted({str(item) for item in record_ids}))
    if len(ids) < 2:
        return []
    rows = frappe.get_all(
        EXCLUSION_DOCTYPE,
        filters={
            "left_record": ["in", ids],
            "right_record": ["in", ids],
            "status": "Active",
        },
        fields=[
            "left_record",
            "right_record",
            "left_fingerprint",
            "right_fingerprint",
        ],
        limit_page_length=100_000,
    )
    return [
        (
            str(row.left_record),
            str(row.right_record),
            str(row.left_fingerprint),
            str(row.right_fingerprint),
        )
        for row in rows
    ]


def preview_materialization(
    *,
    origin: str,
    origin_doctype: str,
    origin_document: str,
    policy_snapshot_json: str | dict[str, Any],
    record_ids: list[str] | tuple[str, ...],
    groups: Iterable[Iterable[str]],
    exclusions: Iterable[tuple[str, str]] = (),
    expected_fingerprints: dict[str, str] | None = None,
    governance_override: bool = False,
) -> dict[str, Any]:
    policy = _policy(policy_snapshot_json)
    plan = build_materialization_plan(
        origin=origin,
        origin_document=origin_document,
        policy_version=policy.version,
        record_ids=record_ids,
        groups=groups,
        exclusions=exclusions,
    )
    existing_decision = frappe.db.get_value(
        DECISION_DOCTYPE, {"decision_key": plan.idempotency_key}, "name"
    )
    records = _record_rows(plan.record_ids)
    for record_id, row in records.items():
        row["source"] = str(row.get("ccd_reg_source") or row.get("source") or "")
    fingerprints = {
        record_id: identity_fingerprint(row, policy)
        for record_id, row in records.items()
    }
    conflicts: set[str] = set()
    stale_records = sorted(
        record_id
        for record_id, expected in (expected_fingerprints or {}).items()
        if fingerprints.get(str(record_id)) != str(expected)
    )
    if stale_records:
        conflicts.add("identity_fingerprint_changed")
    hkid_conflicts = complete_hkid_conflicts(plan.groups, records, policy)
    if hkid_conflicts and not governance_override:
        conflicts.add("complete_hkid_conflict")
    if governance_override and origin != "Governance Override":
        conflicts.add("invalid_governance_override_origin")
    memberships = _current_memberships(plan.record_ids)
    conflicts.update(_existing_group_conflicts(plan.groups, memberships))
    exclusion_conflicts = fingerprint_scoped_exclusion_conflicts(
        plan.groups,
        fingerprints,
        _active_exclusion_rows(plan.record_ids),
    )
    if exclusion_conflicts:
        conflicts.add("active_human_exclusion")

    same_source_groups = []
    for group in plan.groups:
        source_counts = Counter(str(records[item].get("source") or "") for item in group)
        if any(count > 1 for source, count in source_counts.items() if source):
            same_source_groups.append(group)
    if same_source_groups and origin not in HUMAN_ORIGINS:
        conflicts.add("same_source_duplicates_require_human_decision")

    multi_record_groups = [group for group in plan.groups if len(group) > 1]
    return {
        "materializer_version": MATERIALIZER_VERSION,
        "origin": origin,
        "origin_doctype": origin_doctype,
        "origin_document": origin_document,
        "policy_version": policy.version,
        "record_count": len(plan.record_ids),
        "group_count": len(multi_record_groups),
        "membership_count": sum(len(group) for group in multi_record_groups),
        "exclusion_count": len(plan.exclusions),
        "same_source_duplicate_group_count": len(same_source_groups),
        "stale_record_count": len(stale_records),
        "stale_records": stale_records,
        "conflicts": sorted(conflicts),
        "safe": not conflicts,
        "already_applied": bool(existing_decision),
        "identity_decision": str(existing_decision or ""),
        "idempotency_key": plan.idempotency_key,
        "groups": plan.groups,
        "exclusions": plan.exclusions,
        "fingerprints": fingerprints,
        "hkid_conflict_group_count": len(hkid_conflicts),
        "active_exclusion_conflict_count": len(exclusion_conflicts),
    }


def _validate_origin(
    origin: str,
    origin_doctype: str,
    origin_document: str,
) -> dict[str, Any]:
    if origin == "Tiered Evidence":
        if origin_doctype != "CCD Identity Activation Batch":
            frappe.throw("Tiered Evidence must be materialized by an Activation Batch")
        status = frappe.db.get_value(origin_doctype, origin_document, "status")
        if status not in {"Approved", "Applying", "Applied"}:
            frappe.throw("The Activation Batch is not approved")
        return {"status": status}
    elif origin == "Splink Human Review":
        if origin_doctype != "CCD Match Review Candidate":
            frappe.throw("Splink Human Review must originate from a Review Candidate")
        values = frappe.db.get_value(
            origin_doctype,
            origin_document,
            ["review_status", "final_label", "left_record", "right_record"],
            as_dict=True,
        )
        if (
            not values
            or values.review_status not in {"Agreed", "Adjudicated"}
            or values.final_label not in {"Same", "Different"}
        ):
            frappe.throw("The Review Candidate has no final human identity decision")
        return dict(values)
    elif origin == "Component Review":
        if origin_doctype != "CCD Match Component Review":
            frappe.throw("Component Review must originate from a Component Review")
        values = frappe.db.get_value(
            origin_doctype,
            origin_document,
            ["review_status", "final_decision", "final_groups_json"],
            as_dict=True,
        )
        if not values or values.review_status not in {"Agreed", "Adjudicated"} or not values.final_decision:
            frappe.throw("The Component Review is not finalized")
        return dict(values)
    elif origin == "Governance Override":
        _require_manager()
        return {}
    else:
        frappe.throw("Unsupported Identity Decision origin")


def _validate_origin_plan(
    *,
    origin: str,
    origin_document: str,
    provenance: dict[str, Any],
    plan: Any,
    review_context: dict[str, Any] | None,
) -> None:
    decision_type = _decision_type(plan.groups, plan.exclusions)
    if origin == "Tiered Evidence":
        if decision_type != "Same":
            frappe.throw("Tiered Evidence may materialize only a Same decision")
        component = str((review_context or {}).get("component_fingerprint") or "")
        if not component:
            frappe.throw("Tiered Evidence requires its frozen component fingerprint")
        item = frappe.db.get_value(
            "CCD Identity Activation Item",
            {"parent": origin_document, "component_fingerprint": component},
            ["recommendation_names_json"],
            as_dict=True,
        )
        if not item:
            frappe.throw("The component is not part of this Activation Batch")
        recommendation_names = json.loads(item.recommendation_names_json or "[]")
        recommendations = frappe.get_all(
            "CCD Match Recommendation",
            filters={"name": ["in", recommendation_names]},
            fields=["left_record", "right_record", "cluster_fingerprint"],
            limit_page_length=max(len(recommendation_names), 1),
        )
        origin_records = {
            str(record_id)
            for row in recommendations
            for record_id in (row.left_record, row.right_record)
        }
        if (
            len(recommendations) != len(recommendation_names)
            or any(str(row.cluster_fingerprint) != component for row in recommendations)
            or origin_records != set(plan.record_ids)
        ):
            frappe.throw("The Tiered Evidence plan does not match its frozen component")
    elif origin == "Splink Human Review":
        origin_records = {
            str(provenance.get("left_record")),
            str(provenance.get("right_record")),
        }
        if origin_records != set(plan.record_ids):
            frappe.throw("The identity plan does not match the Review Candidate")
        expected_type = "Same" if provenance.get("final_label") == "Same" else "Different"
        if decision_type != expected_type:
            frappe.throw("The identity plan contradicts the final Review Candidate label")
    elif origin == "Component Review":
        final_groups = tuple(
            tuple(str(item) for item in group)
            for group in json.loads(provenance.get("final_groups_json") or "[]")
        )
        if tuple(plan.groups) != tuple(sorted(tuple(sorted(group)) for group in final_groups)):
            frappe.throw("The identity plan contradicts the final component partition")
        expected_types = {
            "All Same": "Same",
            "Partial Match": "Partition",
            "All Different": "Different",
        }
        if decision_type != expected_types.get(str(provenance.get("final_decision"))):
            frappe.throw("The identity plan contradicts the final component decision")


def _decision_type(
    groups: tuple[tuple[str, ...], ...], exclusions: tuple[tuple[str, str], ...]
) -> str:
    if exclusions and all(len(group) == 1 for group in groups):
        return "Different"
    if len(groups) == 1:
        return "Same"
    return "Partition"


def _group_key(decision_key: str, members: tuple[str, ...]) -> str:
    return hashlib.sha256(
        f"{decision_key}\x1fgroup\x1f{'|'.join(sorted(members))}".encode()
    ).hexdigest()


def _membership_key(decision_key: str, group_key: str, record_id: str) -> str:
    return hashlib.sha256(
        f"{decision_key}\x1fmembership\x1f{group_key}\x1f{record_id}".encode()
    ).hexdigest()


def _exclusion_key(
    decision_key: str,
    left: str,
    right: str,
    left_fingerprint: str,
    right_fingerprint: str,
) -> str:
    return hashlib.sha256(
        f"{decision_key}\x1fexclusion\x1f{left}\x1f{right}\x1f{left_fingerprint}\x1f{right_fingerprint}".encode()
    ).hexdigest()


def materialize_identity(
    *,
    origin: str,
    origin_doctype: str,
    origin_document: str,
    policy_snapshot_json: str | dict[str, Any],
    policy_snapshot_sha256: str = "",
    matching_policy: str = "",
    record_ids: list[str] | tuple[str, ...],
    groups: Iterable[Iterable[str]],
    exclusions: Iterable[tuple[str, str]] = (),
    expected_fingerprints: dict[str, str] | None = None,
    reason_codes: Iterable[str] = (),
    review_context: dict[str, Any] | None = None,
    governance_override: bool = False,
    governance_notes: str = "",
    is_demonstration: bool = False,
    require_enabled: bool = True,
) -> dict[str, Any]:
    is_automated = origin == "Tiered Evidence"
    if require_enabled and not materialization_enabled(automated=is_automated):
        frappe.throw(
            "Live identity materialization is disabled"
            + (" or automatic materialization is paused" if is_automated else "")
            + ". Preview remains available."
        )
    if governance_override and not str(governance_notes or "").strip():
        frappe.throw("Governance override notes are required")
    provenance = _validate_origin(origin, origin_doctype, origin_document)
    policy_value = (
        json.loads(policy_snapshot_json)
        if isinstance(policy_snapshot_json, str)
        else policy_snapshot_json
    )
    policy = _policy(policy_value)
    plan = build_materialization_plan(
        origin=origin,
        origin_document=origin_document,
        policy_version=policy.version,
        record_ids=record_ids,
        groups=groups,
        exclusions=exclusions,
    )
    _validate_origin_plan(
        origin=origin,
        origin_document=origin_document,
        provenance=provenance,
        plan=plan,
        review_context=review_context,
    )
    _lock_records(plan.record_ids)
    preview = preview_materialization(
        origin=origin,
        origin_doctype=origin_doctype,
        origin_document=origin_document,
        policy_snapshot_json=policy_value,
        record_ids=plan.record_ids,
        groups=plan.groups,
        exclusions=plan.exclusions,
        expected_fingerprints=expected_fingerprints,
        governance_override=governance_override,
    )
    if preview["already_applied"]:
        return {
            "status": "Already Applied",
            "identity_decision": preview["identity_decision"],
            "created_groups": 0,
            "created_memberships": 0,
            "created_exclusions": 0,
            "idempotency_key": preview["idempotency_key"],
        }
    if not preview["safe"]:
        frappe.throw("Identity safety checks failed: " + ", ".join(preview["conflicts"]))

    now = frappe.utils.now_datetime()
    fingerprints = dict(preview["fingerprints"])
    decision = frappe.get_doc(
        {
            "doctype": DECISION_DOCTYPE,
            "decision_key": plan.idempotency_key,
            "decision_version": 1,
            "decision_type": _decision_type(plan.groups, plan.exclusions),
            "origin": origin,
            "origin_doctype": origin_doctype,
            "origin_document": origin_document,
            "matching_policy": matching_policy or None,
            "policy_version": policy.version,
            "policy_snapshot_sha256": policy_snapshot_sha256,
            "policy_snapshot_json": _json(policy_value),
            "participant_records_json": _json(plan.record_ids),
            "participant_fingerprints_json": _json(fingerprints),
            "final_groups_json": _json(plan.groups),
            "exclusions_json": _json(plan.exclusions),
            "reason_codes_json": _json(sorted(set(str(item) for item in reason_codes))),
            "review_context_json": _json(
                {
                    **(review_context or {}),
                    **({"governance_notes": governance_notes} if governance_override else {}),
                }
            ),
            "safety_outcome": "Governance Override" if governance_override else "Passed",
            "status": "Active",
            "decided_at": now,
            "decided_by": frappe.session.user,
        }
    ).insert(ignore_permissions=True)
    _append_event(
        entity_doctype=DECISION_DOCTYPE,
        entity_name=decision.name,
        event_type="Create",
        reason=f"identity_decision_from:{origin}",
        nonce=plan.idempotency_key,
        to_status="Active",
        identity_decision=decision.name,
        metadata={"materializer_version": MATERIALIZER_VERSION},
        is_demonstration=is_demonstration,
    )

    records = _record_rows(plan.record_ids)
    existing = _current_memberships(plan.record_ids)
    existing_by_record = {str(row.ccd_master): row for row in existing}
    created_groups = created_memberships = 0
    group_names: list[str] = []
    for members in plan.groups:
        if len(members) < 2:
            continue
        existing_groups = {
            str(existing_by_record[item].identity_group)
            for item in members
            if item in existing_by_record
        }
        if existing_groups:
            group_name = next(iter(existing_groups))
            group = frappe.get_doc(GROUP_DOCTYPE, group_name)
        else:
            key = _group_key(plan.idempotency_key, members)
            source_counts = Counter(
                str(records[item].get("ccd_reg_source") or "") for item in members
            )
            group_fingerprint = hashlib.sha256(
                "\x1f".join(sorted(fingerprints[item] for item in members)).encode()
            ).hexdigest()
            group = frappe.get_doc(
                {
                    "doctype": GROUP_DOCTYPE,
                    "group_key": key,
                    "group_version": 1,
                    "status": "Active",
                    "originating_decision": decision.name,
                    "group_fingerprint": group_fingerprint,
                    "active_member_count": 0,
                    "same_source_duplicate_warning": int(
                        any(count > 1 for source, count in source_counts.items() if source)
                    ),
                    "created_at": now,
                    "last_validation_at": now,
                }
            ).insert(ignore_permissions=True)
            group_name = group.name
            created_groups += 1
            _append_event(
                entity_doctype=GROUP_DOCTYPE,
                entity_name=group.name,
                event_type="Create",
                reason="identity_group_materialized",
                nonce=plan.idempotency_key,
                to_status="Active",
                identity_decision=decision.name,
                identity_group=group.name,
                is_demonstration=is_demonstration,
            )
        group_names.append(group_name)
        for record_id in members:
            if record_id in existing_by_record:
                continue
            key = _membership_key(plan.idempotency_key, str(group.group_key), record_id)
            membership = frappe.get_doc(
                {
                    "doctype": MEMBERSHIP_DOCTYPE,
                    "membership_key": key,
                    "identity_group": group_name,
                    "ccd_master": record_id,
                    "governed_source": str(records[record_id].get("ccd_reg_source") or ""),
                    "identity_fingerprint": fingerprints[record_id],
                    "status": "Active",
                    "valid_from": now,
                    "last_validated_at": now,
                    "originating_decision": decision.name,
                }
            ).insert(ignore_permissions=True)
            created_memberships += 1
            _append_event(
                entity_doctype=MEMBERSHIP_DOCTYPE,
                entity_name=membership.name,
                event_type="Activate",
                reason="identity_membership_materialized",
                nonce=plan.idempotency_key,
                to_status="Active",
                identity_decision=decision.name,
                identity_group=group_name,
                identity_membership=membership.name,
                is_demonstration=is_demonstration,
            )
        active_count = frappe.db.count(
            MEMBERSHIP_DOCTYPE,
            {"identity_group": group_name, "status": ["in", CURRENT_MEMBERSHIP_STATUSES]},
        )
        frappe.db.set_value(
            GROUP_DOCTYPE,
            group_name,
            {"active_member_count": active_count, "last_validation_at": now},
            update_modified=False,
        )

    created_exclusions = 0
    for left, right in plan.exclusions:
        key = _exclusion_key(
            plan.idempotency_key,
            left,
            right,
            fingerprints[left],
            fingerprints[right],
        )
        if frappe.db.exists(EXCLUSION_DOCTYPE, {"exclusion_key": key}):
            continue
        frappe.get_doc(
            {
                "doctype": EXCLUSION_DOCTYPE,
                "exclusion_key": key,
                "left_record": left,
                "right_record": right,
                "left_fingerprint": fingerprints[left],
                "right_fingerprint": fingerprints[right],
                "originating_decision": decision.name,
                "status": "Active",
                "reason": "human_confirmed_different",
                "decided_at": now,
                "decided_by": frappe.session.user,
            }
        ).insert(ignore_permissions=True)
        created_exclusions += 1

    return {
        "status": "Applied",
        "identity_decision": decision.name,
        "identity_groups": group_names,
        "created_groups": created_groups,
        "created_memberships": created_memberships,
        "created_exclusions": created_exclusions,
        "idempotency_key": plan.idempotency_key,
    }


def _decision_policy(decision_name: str) -> MatchingPolicy:
    snapshot = frappe.db.get_value(DECISION_DOCTYPE, decision_name, "policy_snapshot_json")
    if not snapshot:
        frappe.throw("The Identity Decision has no reproducible policy snapshot")
    return _policy(snapshot)


def handle_ccd_master_update(doc: Any, method: str | None = None) -> None:
    """Mark only governed identity changes for explicit revalidation."""
    if not frappe.db.table_exists(MEMBERSHIP_DOCTYPE):
        return
    memberships = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={"ccd_master": doc.name, "status": "Active"},
        fields=["name", "identity_group", "identity_fingerprint", "originating_decision"],
        limit=2,
    )
    if not memberships:
        return
    membership = memberships[0]
    policy = _decision_policy(membership.originating_decision)
    current = dict(doc.as_dict())
    current["source"] = str(current.get("ccd_reg_source") or "")
    fingerprint = identity_fingerprint(current, policy)
    if fingerprint == str(membership.identity_fingerprint):
        return
    frappe.db.set_value(
        MEMBERSHIP_DOCTYPE,
        membership.name,
        "status",
        "Needs Revalidation",
        update_modified=False,
    )
    frappe.db.set_value(
        GROUP_DOCTYPE,
        membership.identity_group,
        "status",
        "Needs Revalidation",
        update_modified=False,
    )
    nonce = hashlib.sha256(
        f"{membership.name}\x1f{fingerprint}".encode()
    ).hexdigest()
    _append_event(
        entity_doctype=MEMBERSHIP_DOCTYPE,
        entity_name=membership.name,
        event_type="Needs Revalidation",
        reason="governed_identity_fingerprint_changed",
        nonce=nonce,
        from_status="Active",
        to_status="Needs Revalidation",
        identity_decision=membership.originating_decision,
        identity_group=membership.identity_group,
        identity_membership=membership.name,
        metadata={"new_fingerprint": fingerprint},
    )


@frappe.whitelist()
def get_identity_resolution(ccd_master_name: str) -> dict[str, Any]:
    _require_identity_reader(ccd_master_name)
    rows = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "ccd_master": ccd_master_name,
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=[
            "name",
            "identity_group",
            "identity_fingerprint",
            "status",
            "originating_decision",
            "valid_from",
            "last_validated_at",
        ],
        order_by="valid_from desc",
        limit=1,
    )
    if not rows:
        return {
            "status": "Unlinked",
            "physical_merge": False,
            "message": "This CCD Master is not in an active Identity Group.",
        }
    membership = rows[0]
    group = frappe.get_doc(GROUP_DOCTYPE, membership.identity_group)
    decision = frappe.get_doc(DECISION_DOCTYPE, membership.originating_decision)
    members = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "identity_group": group.name,
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=["name", "ccd_master", "governed_source", "status", "valid_from"],
        order_by="valid_from, name",
        limit_page_length=100_000,
    )
    sensitive = _has_sensitive_access()
    member_payload = []
    for index, row in enumerate(members, start=1):
        item = {
            "alias": f"Member {index}",
            "source": row.governed_source or "",
            "status": row.status,
            "is_this_record": str(row.ccd_master) == str(ccd_master_name),
        }
        if sensitive:
            item.update({"record_id": row.ccd_master, "membership": row.name})
        member_payload.append(item)

    current_doc = frappe.get_doc("CCD Master", ccd_master_name).as_dict()
    current_doc["source"] = str(current_doc.get("ccd_reg_source") or "")
    current_fingerprint = identity_fingerprint(
        current_doc, _decision_policy(decision.name)
    )
    fingerprint_changed = current_fingerprint != str(membership.identity_fingerprint)
    return {
        "status": (
            "Needs Revalidation"
            if fingerprint_changed or membership.status == "Needs Revalidation"
            else "Linked"
        ),
        "physical_merge": False,
        "identity_group": group.name,
        "group_status": group.status,
        "membership": membership.name if sensitive else "",
        "members": member_payload,
        "decision": decision.name,
        "decision_origin": decision.origin,
        "decision_type": decision.decision_type,
        "policy_version": decision.policy_version,
        "decided_at": decision.decided_at,
        "fingerprint_changed": fingerprint_changed,
        "same_source_duplicate_warning": bool(group.same_source_duplicate_warning),
        "sensitive_values_visible": sensitive,
    }


@frappe.whitelist()
def revalidate_membership(membership_name: str) -> dict[str, str]:
    _require_manager()
    membership = frappe.get_doc(MEMBERSHIP_DOCTYPE, membership_name)
    if membership.status != "Needs Revalidation":
        frappe.throw("Only a Membership needing revalidation can be revalidated")
    group_members = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "identity_group": membership.identity_group,
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=["name", "ccd_master", "identity_fingerprint", "originating_decision"],
        limit_page_length=100_000,
    )
    _lock_records(row.ccd_master for row in group_members)
    policy = _decision_policy(membership.originating_decision)
    records = _record_rows(row.ccd_master for row in group_members)
    for record_id, row in records.items():
        row["source"] = str(row.get("ccd_reg_source") or "")
    conflicts = complete_hkid_conflicts(
        (tuple(records),), records, policy
    )
    if conflicts:
        frappe.throw("Revalidation failed: complete HKID conflict")
    current = identity_fingerprint(records[membership.ccd_master], policy)
    now = frappe.utils.now_datetime()
    frappe.db.set_value(
        MEMBERSHIP_DOCTYPE,
        membership.name,
        {
            "identity_fingerprint": current,
            "status": "Active",
            "last_validated_at": now,
        },
        update_modified=False,
    )
    remaining = frappe.db.count(
        MEMBERSHIP_DOCTYPE,
        {"identity_group": membership.identity_group, "status": "Needs Revalidation"},
    )
    if remaining == 0:
        frappe.db.set_value(
            GROUP_DOCTYPE,
            membership.identity_group,
            {"status": "Active", "last_validation_at": now},
            update_modified=False,
        )
    _append_event(
        entity_doctype=MEMBERSHIP_DOCTYPE,
        entity_name=membership.name,
        event_type="Revalidate",
        reason="manager_revalidation_passed",
        nonce=current,
        from_status="Needs Revalidation",
        to_status="Active",
        identity_decision=membership.originating_decision,
        identity_group=membership.identity_group,
        identity_membership=membership.name,
    )
    frappe.db.commit()
    return {"membership": membership.name, "status": "Active"}


@frappe.whitelist()
def end_membership(
    membership_name: str,
    reason: str,
    is_demonstration: int | str = 0,
) -> dict[str, str]:
    _require_manager()
    if not str(reason or "").strip():
        frappe.throw("An end reason is required")
    membership = frappe.get_doc(MEMBERSHIP_DOCTYPE, membership_name)
    if membership.status not in CURRENT_MEMBERSHIP_STATUSES:
        frappe.throw("Only a current Membership can be ended")
    now = frappe.utils.now_datetime()
    old_status = membership.status
    frappe.db.set_value(
        MEMBERSHIP_DOCTYPE,
        membership.name,
        {
            "status": "Ended",
            "valid_to": now,
            "ended_reason": str(reason).strip(),
            "ended_by": frappe.session.user,
        },
        update_modified=False,
    )
    active_count = frappe.db.count(
        MEMBERSHIP_DOCTYPE,
        {"identity_group": membership.identity_group, "status": ["in", CURRENT_MEMBERSHIP_STATUSES]},
    )
    frappe.db.set_value(
        GROUP_DOCTYPE,
        membership.identity_group,
        {
            "active_member_count": active_count,
            "status": "Ended" if active_count == 0 else "Needs Revalidation",
        },
        update_modified=False,
    )
    _append_event(
        entity_doctype=MEMBERSHIP_DOCTYPE,
        entity_name=membership.name,
        event_type="End",
        reason=str(reason).strip(),
        nonce=str(now),
        from_status=old_status,
        to_status="Ended",
        identity_decision=membership.originating_decision,
        identity_group=membership.identity_group,
        identity_membership=membership.name,
        is_demonstration=bool(int(is_demonstration or 0)),
    )
    frappe.db.commit()
    return {"membership": membership.name, "status": "Ended"}
