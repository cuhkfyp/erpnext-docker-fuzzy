"""Zero-write preview and atomic resolution of complete identity overlaps."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

import frappe

from db_connector.api_fuzzy_canary import (
    _change_recommendation_status,
    _display_evidence_value,
    _has_sensitive_access,
    _refresh_run_counts,
    _snapshot_hash,
)
from db_connector.api_identity_activation import _component_context
from db_connector.api_identity_correction import (
    _active_exclusions,
    _current_partition,
    _expand_complete_scope,
    _link_replacement_groups,
    _lock_named_rows,
    _mark_origin_corrected,
    _rows_as_dicts,
)
from db_connector.api_identity_human import (
    FINAL_REVIEW_STATUSES,
    _candidate_materialization_plan,
    _component_materialization_plan,
)
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
    exclusions_for_partition,
    normalize_partition,
    stable_payload_fingerprint,
)
from db_connector.fuzzy_matching.identity import (
    complete_hkid_conflicts,
    identity_fingerprint,
)
from db_connector.fuzzy_matching.overlap import (
    conflicting_different_pairs,
    constraint_partition,
    partition_splits_groups,
)


RESOLUTION_DOCTYPE = "CCD Identity Overlap Resolution"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
COMPONENT_DOCTYPE = "CCD Match Component Review"
ACTIVATION_BATCH_DOCTYPE = "CCD Identity Activation Batch"
ACTIVATION_ITEM_DOCTYPE = "CCD Identity Activation Item"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
SUPPORTED_SEED_DOCTYPES = {
    CANDIDATE_DOCTYPE,
    COMPONENT_DOCTYPE,
    ACTIVATION_ITEM_DOCTYPE,
}
MAX_INCLUDED_PENDING_SCOPES = 100
MAX_ADJACENT_EVIDENCE_ROWS = 200


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _as_bool(value: Any) -> bool:
    return str(value or "0").strip().casefold() in {"1", "true", "yes", "on"}


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _load_list(value: Any, label: str) -> list[Any]:
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw(f"{label} must be valid JSON")
    if not isinstance(loaded, (list, tuple)):
        frappe.throw(f"{label} must be a JSON list")
    return list(loaded)


def _source_key(doctype: str, name: str) -> str:
    return f"{doctype}:{name}"


def _normalize_groups(groups: Iterable[Iterable[str]]) -> tuple[tuple[str, ...], ...]:
    records = sorted({str(item) for group in groups for item in group})
    return normalize_partition(records, groups)


def _validated_policy_snapshot(document: Any) -> tuple[str, str]:
    snapshot_json = str(document.policy_snapshot_json or "").strip()
    snapshot_sha256 = str(document.policy_snapshot_sha256 or "").strip()
    if not snapshot_json or not snapshot_sha256:
        frappe.throw(f"{document.doctype} {document.name} has no frozen policy snapshot")
    if _snapshot_hash(snapshot_json) != snapshot_sha256:
        frappe.throw(f"{document.doctype} {document.name} has a corrupt frozen policy snapshot")
    return snapshot_json, snapshot_sha256


def _candidate_scope(name: str) -> dict[str, Any]:
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, name)
    if (
        candidate.review_status not in FINAL_REVIEW_STATUSES
        or candidate.final_label not in {"Same", "Different"}
    ):
        frappe.throw("The Splink candidate is not finalized")
    if candidate.stale:
        frappe.throw("The Splink candidate is stale")
    if candidate.materialization_status not in {"Pending", "Exception"}:
        frappe.throw("Only Pending or Exception Splink candidates can start overlap resolution")
    plan = _candidate_materialization_plan(candidate)
    policy_snapshot_json, policy_snapshot_sha256 = _validated_policy_snapshot(plan["run"])
    groups = _normalize_groups(plan["groups"])
    return {
        "key": _source_key(CANDIDATE_DOCTYPE, candidate.name),
        "doctype": CANDIDATE_DOCTYPE,
        "name": str(candidate.name),
        "origin": "Splink Human Review",
        "result": str(candidate.final_label),
        "records": tuple(sorted(str(item) for item in plan["record_ids"])),
        "groups": groups,
        "exclusions": tuple(sorted(tuple(sorted(pair)) for pair in plan["exclusions"])),
        "policy_snapshot_json": policy_snapshot_json,
        "policy_snapshot_sha256": policy_snapshot_sha256,
        "matching_policy": plan["run"].matching_policy,
        "policy_version": plan["run"].policy_version,
        "expected_fingerprints": dict(plan["expected_fingerprints"]),
        "expected_modified": dict(plan["expected_modified"]),
        "probability": float(candidate.probabilistic_score or 0),
        "recommendation_ids": (),
        "source_doctype": str(plan["run"].doctype),
        "source_document": str(plan["run"].name),
    }


def _component_scope(name: str) -> dict[str, Any]:
    review = frappe.get_doc(COMPONENT_DOCTYPE, name)
    if review.review_status not in FINAL_REVIEW_STATUSES or not review.final_decision:
        frappe.throw("The Exception component is not finalized")
    if review.stale:
        frappe.throw("The Exception component is stale")
    if review.materialization_status not in {"Pending", "Exception"}:
        frappe.throw("Only Pending or Exception components can start overlap resolution")
    plan = _component_materialization_plan(review)
    policy_snapshot_json, policy_snapshot_sha256 = _validated_policy_snapshot(plan["canary"])
    groups = _normalize_groups(plan["groups"])
    recommendation_ids = tuple(
        sorted(
            str(value)
            for value in frappe.get_all(
                RECOMMENDATION_DOCTYPE,
                filters={
                    "canary_run": review.canary_run,
                    "cluster_fingerprint": review.cluster_fingerprint,
                },
                pluck="name",
                limit_page_length=100_000,
            )
        )
    )
    return {
        "key": _source_key(COMPONENT_DOCTYPE, review.name),
        "doctype": COMPONENT_DOCTYPE,
        "name": str(review.name),
        "origin": "Component Review",
        "result": str(review.final_decision),
        "records": tuple(sorted(str(item) for item in plan["record_ids"])),
        "groups": groups,
        "exclusions": tuple(sorted(tuple(sorted(pair)) for pair in plan["exclusions"])),
        "policy_snapshot_json": policy_snapshot_json,
        "policy_snapshot_sha256": policy_snapshot_sha256,
        "matching_policy": plan["canary"].matching_policy,
        "policy_version": plan["canary"].policy_version,
        "expected_fingerprints": dict(plan["expected_fingerprints"]),
        "expected_modified": dict(plan["expected_modified"]),
        "probability": None,
        "recommendation_ids": recommendation_ids,
        "source_doctype": str(plan["canary"].doctype),
        "source_document": str(plan["canary"].name),
        "canary_run": str(plan["canary"].name),
    }


def _activation_scope(name: str) -> dict[str, Any]:
    item = frappe.get_doc(ACTIVATION_ITEM_DOCTYPE, name)
    batch = frappe.get_doc(ACTIVATION_BATCH_DOCTYPE, item.parent)
    if batch.status not in {"Reviewed", "Approved"}:
        frappe.throw(
            "The Tiered component's Activation Batch must be Reviewed or Approved"
        )
    if item.status not in {"Planned", "Failed", "Exception"}:
        frappe.throw("Only an unapplied Activation Item can start overlap resolution")
    canary = frappe.get_doc("CCD Match Canary Run", batch.canary_run)
    policy_snapshot_json, policy_snapshot_sha256 = _validated_policy_snapshot(canary)
    if str(batch.policy_snapshot_sha256 or "") != policy_snapshot_sha256:
        frappe.throw("The Activation Batch and canary policy snapshots differ")
    recommendation_ids = _load_list(
        item.recommendation_names_json or "[]", "Frozen recommendation selection"
    )
    if not recommendation_ids or any(not isinstance(value, str) for value in recommendation_ids):
        frappe.throw("The Activation Item has an invalid frozen recommendation selection")
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"name": ["in", recommendation_ids]},
        fields=[
            "name",
            "canary_run",
            "cluster_fingerprint",
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
            "left_identity_fingerprint",
            "right_identity_fingerprint",
            "status",
            "rollout_state",
            "reason_codes_json",
            "safety_reasons_json",
        ],
        limit_page_length=max(len(recommendation_ids), 1),
    )
    if (
        len(rows) != len(recommendation_ids)
        or any(str(row.canary_run) != str(batch.canary_run) for row in rows)
        or any(str(row.cluster_fingerprint) != str(item.component_fingerprint) for row in rows)
        or any(str(row.status) != "Proposed" or str(row.rollout_state) == "Held" for row in rows)
    ):
        frappe.throw("The frozen Tiered component is no longer fully available")
    context = _component_context(rows)
    records = tuple(sorted(str(value) for value in context["record_ids"]))
    return {
        "key": _source_key(ACTIVATION_ITEM_DOCTYPE, item.name),
        "doctype": ACTIVATION_ITEM_DOCTYPE,
        "name": str(item.name),
        "origin": "Tiered Evidence",
        "result": "All Same",
        "records": records,
        "groups": (records,),
        "exclusions": (),
        "policy_snapshot_json": policy_snapshot_json,
        "policy_snapshot_sha256": policy_snapshot_sha256,
        "matching_policy": str(batch.matching_policy or ""),
        "policy_version": str(batch.policy_version or ""),
        "expected_fingerprints": dict(context["expected_fingerprints"]),
        "expected_modified": dict(context["expected_modified"]),
        "probability": None,
        "recommendation_ids": tuple(sorted(str(value) for value in recommendation_ids)),
        "activation_batch": str(batch.name),
        "activation_batch_status": str(batch.status),
        "canary_run": str(batch.canary_run),
        "source_doctype": str(canary.doctype),
        "source_document": str(canary.name),
        "is_demonstration": bool(batch.is_demonstration),
    }


def _load_scope(doctype: str, name: str) -> dict[str, Any]:
    if doctype == CANDIDATE_DOCTYPE:
        return _candidate_scope(name)
    if doctype == COMPONENT_DOCTYPE:
        return _component_scope(name)
    if doctype == ACTIVATION_ITEM_DOCTYPE:
        return _activation_scope(name)
    frappe.throw("Unsupported overlap-resolution seed DocType")


def _require_activation_seed_approved_for_apply(
    seed_doctype: str, seed_document: str
) -> None:
    """Keep Reviewed Activation Items preview-only until explicit approval."""
    if seed_doctype != ACTIVATION_ITEM_DOCTYPE:
        return
    item = frappe.get_doc(ACTIVATION_ITEM_DOCTYPE, seed_document)
    batch_status = frappe.db.get_value(
        ACTIVATION_BATCH_DOCTYPE, item.parent, "status"
    )
    if str(batch_status or "") != "Approved":
        frappe.throw(
            "Approve the frozen Activation Batch before applying an overlap resolution"
        )


def _connected_finalized_pending_scopes(
    record_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Load only decision-ready scopes touching the current expansion frontier."""
    records = {str(item) for item in record_ids}
    scopes: list[dict[str, Any]] = []
    candidates = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={
            "review_status": ["in", sorted(FINAL_REVIEW_STATUSES)],
            "final_label": ["in", ["Same", "Different"]],
            "stale": 0,
            "materialization_status": ["in", ["Pending", "Exception"]],
        },
        fields=["name", "left_record", "right_record"],
        limit_page_length=100_000,
    )
    for row in candidates:
        if records.intersection((str(row.left_record), str(row.right_record))):
            scopes.append(_candidate_scope(str(row.name)))

    component_records: dict[str, set[str]] = {}
    for row in frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"component_review": ["is", "set"]},
        fields=["component_review", "left_record", "right_record"],
        limit_page_length=100_000,
    ):
        component_records.setdefault(str(row.component_review), set()).update(
            (str(row.left_record), str(row.right_record))
        )
    connected_components = sorted(
        name for name, members in component_records.items() if records.intersection(members)
    )
    components = frappe.get_all(
        COMPONENT_DOCTYPE,
        filters={
            "name": ["in", connected_components or [""]],
            "review_status": ["in", sorted(FINAL_REVIEW_STATUSES)],
            "stale": 0,
            "materialization_status": ["in", ["Pending", "Exception"]],
        },
        pluck="name",
        limit_page_length=100_000,
    )
    for name in components:
        scopes.append(_component_scope(str(name)))

    approved_batches = frappe.get_all(
        ACTIVATION_BATCH_DOCTYPE,
        filters={"status": "Approved"},
        pluck="name",
        limit_page_length=100_000,
    )
    items = frappe.get_all(
        ACTIVATION_ITEM_DOCTYPE,
        filters={
            "parent": ["in", approved_batches or [""]],
            "status": ["in", ["Planned", "Failed", "Exception"]],
        },
        fields=["name", "recommendation_names_json"],
        limit_page_length=100_000,
    )
    frozen_names: set[str] = set()
    names_by_item: dict[str, list[str]] = {}
    for item in items:
        try:
            names = _load_list(
                item.recommendation_names_json or "[]",
                f"Activation Item {item.name} recommendation selection",
            )
        except frappe.ValidationError:
            # A disconnected corrupt item must not block another complete scope.
            continue
        clean_names = [str(value) for value in names if isinstance(value, str)]
        names_by_item[str(item.name)] = clean_names
        frozen_names.update(clean_names)
    recommendation_records = {
        str(row.name): {str(row.left_record), str(row.right_record)}
        for row in frappe.get_all(
            RECOMMENDATION_DOCTYPE,
            filters={"name": ["in", sorted(frozen_names) or [""]]},
            fields=["name", "left_record", "right_record"],
            limit_page_length=100_000,
        )
    }
    for item_name, recommendation_names in names_by_item.items():
        if any(
            records.intersection(recommendation_records.get(name, set()))
            for name in recommendation_names
        ):
            scopes.append(_activation_scope(item_name))
    return sorted(scopes, key=lambda row: row["key"])


def _active_exclusions_touching(record_ids: Iterable[str]) -> list[Any]:
    ids = {str(item) for item in record_ids}
    return [
        row
        for row in frappe.get_all(
            EXCLUSION_DOCTYPE,
            filters={"status": "Active"},
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
        if str(row.left_record) in ids or str(row.right_record) in ids
    ]


def _expand_authoritative_scope(
    seed: dict[str, Any],
) -> tuple[set[str], list[Any], list[Any], list[dict[str, Any]]]:
    records = set(seed["records"])
    included = {seed["key"]: seed}
    memberships: list[Any] = []
    touching_exclusions: list[Any] = []
    while True:
        prior_records = set(records)
        prior_included = set(included)
        expanded_records, memberships = _expand_complete_scope(records)
        records = set(expanded_records)
        touching_exclusions = _active_exclusions_touching(records)
        records.update(
            str(value)
            for row in touching_exclusions
            for value in (row.left_record, row.right_record)
        )
        for scope in _connected_finalized_pending_scopes(records):
            if set(scope["records"]).intersection(records):
                included[scope["key"]] = scope
                records.update(scope["records"])
        if len(records) > MAX_CORRECTION_RECORDS:
            frappe.throw(
                f"The complete overlap scope exceeds the bounded {MAX_CORRECTION_RECORDS}-record limit"
            )
        if len(included) > MAX_INCLUDED_PENDING_SCOPES:
            frappe.throw(
                f"The complete overlap scope exceeds the bounded {MAX_INCLUDED_PENDING_SCOPES}-source limit"
            )
        if records == prior_records and set(included) == prior_included:
            break
    return records, memberships, touching_exclusions, sorted(included.values(), key=lambda row: row["key"])


def _scope_summary(scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": scope["key"],
        "doctype": scope["doctype"],
        "document": scope["name"],
        "origin": scope["origin"],
        "result": scope["result"],
        "records": scope["records"],
        "groups": scope["groups"],
        "exclusions": scope["exclusions"],
        "probability": scope.get("probability"),
        "recommendations": scope.get("recommendation_ids", ()),
        "activation_batch": scope.get("activation_batch", ""),
        "canary_run": scope.get("canary_run", ""),
    }


def _adjacent_unreviewed(
    record_ids: Iterable[str], included: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    records = set(str(item) for item in record_ids)
    included_keys = {scope["key"] for scope in included}
    adjacent: dict[str, dict[str, Any]] = {}
    for row in frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={"stale": 0},
        fields=[
            "name",
            "left_record",
            "right_record",
            "review_status",
            "final_label",
            "materialization_status",
            "probabilistic_score",
        ],
        limit_page_length=100_000,
    ):
        key = _source_key(CANDIDATE_DOCTYPE, str(row.name))
        if key in included_keys or not records.intersection(
            {str(row.left_record), str(row.right_record)}
        ):
            continue
        if str(row.review_status) in FINAL_REVIEW_STATUSES:
            continue
        adjacent[key] = {
            "key": key,
            "doctype": CANDIDATE_DOCTYPE,
            "document": str(row.name),
            "origin": "Splink Human Review",
            "status": str(row.review_status or "Unreviewed"),
            "result": str(row.final_label or ""),
            "probability": float(row.probabilistic_score or 0),
            "records": tuple(sorted((str(row.left_record), str(row.right_record)))),
            "documents": (str(row.name),),
        }

    component_records: dict[str, set[str]] = {}
    for row in frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"component_review": ["is", "set"]},
        fields=["component_review", "left_record", "right_record"],
        limit_page_length=100_000,
    ):
        component_records.setdefault(str(row.component_review), set()).update(
            (str(row.left_record), str(row.right_record))
        )
    if component_records:
        rows = frappe.get_all(
            COMPONENT_DOCTYPE,
            filters={"name": ["in", sorted(component_records)]},
            fields=[
                "name",
                "review_status",
                "final_decision",
                "materialization_status",
                "stale",
            ],
            limit_page_length=100_000,
        )
        for row in rows:
            key = _source_key(COMPONENT_DOCTYPE, str(row.name))
            members = component_records[str(row.name)]
            if key in included_keys or not records.intersection(members) or row.stale:
                continue
            if str(row.review_status) in FINAL_REVIEW_STATUSES:
                continue
            adjacent[key] = {
                "key": key,
                "doctype": COMPONENT_DOCTYPE,
                "document": str(row.name),
                "origin": "Component Review",
                "status": str(row.review_status or "Unreviewed"),
                "result": str(row.final_decision or ""),
                "probability": None,
                "records": tuple(sorted(members)),
                "documents": (str(row.name),),
            }

    included_recommendations = {
        value for scope in included for value in scope.get("recommendation_ids", ())
    }
    tiered_components: dict[tuple[str, str], dict[str, Any]] = {}
    for row in frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"status": "Proposed"},
        fields=[
            "name",
            "canary_run",
            "cluster_fingerprint",
            "left_record",
            "right_record",
            "rollout_state",
        ],
        limit_page_length=100_000,
    ):
        if str(row.name) in included_recommendations:
            continue
        pair_records = {str(row.left_record), str(row.right_record)}
        if not pair_records.intersection(records):
            continue
        component_key = (str(row.canary_run), str(row.cluster_fingerprint))
        item = tiered_components.setdefault(
            component_key,
            {
                "records": set(),
                "documents": [],
                "held": False,
            },
        )
        item["records"].update(pair_records)
        item["documents"].append(str(row.name))
        item["held"] = item["held"] or str(row.rollout_state) == "Held"
    for (run_name, component), item in tiered_components.items():
        key = f"Tiered Proposal:{run_name}:{component}"
        adjacent[key] = {
            "key": key,
            "doctype": RECOMMENDATION_DOCTYPE,
            "document": ";".join(sorted(item["documents"])),
            "origin": "Tiered Evidence",
            "status": "Held" if item["held"] else "Proposed",
            "result": "Proposed Same",
            "probability": None,
            "records": tuple(sorted(item["records"])),
            "documents": tuple(sorted(item["documents"])),
        }

    values = sorted(adjacent.values(), key=lambda row: row["key"])
    return values[:MAX_ADJACENT_EVIDENCE_ROWS], len(values)


def _validate_frozen_sources(
    included: Iterable[dict[str, Any]], record_rows: dict[str, dict[str, Any]]
) -> list[str]:
    conflicts: set[str] = set()
    for scope in included:
        policy = _policy(scope["policy_snapshot_json"])
        expected_fingerprints = scope["expected_fingerprints"]
        expected_modified = scope["expected_modified"]
        for record_id in scope["records"]:
            if record_id not in expected_fingerprints or record_id not in expected_modified:
                conflicts.add(f"frozen_snapshot_incomplete:{scope['key']}:{record_id}")
                continue
            row = dict(record_rows[record_id])
            row["source"] = str(row.get("ccd_reg_source") or row.get("source") or "")
            if identity_fingerprint(row, policy) != str(expected_fingerprints[record_id]):
                conflicts.add(f"identity_fingerprint_changed:{scope['key']}:{record_id}")
            if str(row.get("modified") or "") != str(expected_modified[record_id]):
                conflicts.add(f"source_modified_after_snapshot:{scope['key']}:{record_id}")
    return sorted(conflicts)


def _combined_context(seed_doctype: str, seed_document: str) -> dict[str, Any]:
    seed_doctype = str(seed_doctype or "").strip()
    seed_document = str(seed_document or "").strip()
    if seed_doctype not in SUPPORTED_SEED_DOCTYPES or not seed_document:
        frappe.throw("Select a finalized pending Splink, Component, or Activation Item")
    seed = _load_scope(seed_doctype, seed_document)
    records, memberships, _touching, included = _expand_authoritative_scope(seed)
    record_ids = tuple(sorted(records))
    record_rows = _record_rows(record_ids)
    frozen_conflicts = _validate_frozen_sources(included, record_rows)
    settings = frappe.get_single("CCD Identity Resolution Settings")
    if bool(settings.automation_paused):
        frozen_conflicts.append("identity_automation_circuit_breaker_paused")

    group_names = sorted({str(row.identity_group) for row in memberships})
    groups = (
        frappe.get_all(
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
        )
        if group_names
        else []
    )
    if len(groups) != len(group_names):
        frappe.throw("The combined scope references a missing active Identity Group")
    exclusions = _active_exclusions(record_ids)
    decision_names = {
        *(str(row.originating_decision) for row in memberships),
        *(str(row.originating_decision) for row in groups),
        *(str(row.originating_decision) for row in exclusions),
    }
    decisions = (
        frappe.get_all(
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
        if decision_names
        else []
    )
    if len(decisions) != len(decision_names):
        frappe.throw("The combined scope references a missing Identity Decision")

    current_groups = _current_partition(record_ids, memberships)
    prior_same_groups = [
        tuple(sorted(str(row.ccd_master) for row in memberships if str(row.identity_group) == group_name))
        for group_name in group_names
    ]
    same_constraints = [*prior_same_groups]
    different_constraints = [
        tuple(sorted((str(row.left_record), str(row.right_record))))
        for row in exclusions
    ]
    for scope in included:
        same_constraints.extend(group for group in scope["groups"] if len(group) > 1)
        different_constraints.extend(scope["exclusions"])
    default_groups = constraint_partition(record_ids, same_constraints)
    constraint_conflicts = conflicting_different_pairs(
        default_groups, different_constraints
    )
    adjacent, adjacent_total = _adjacent_unreviewed(record_ids, included)

    seed_policy = _policy(seed["policy_snapshot_json"])
    sensitive_values_visible = _has_sensitive_access()
    evidence_attributes = list(seed_policy.attributes())
    seed_fingerprints = {}
    modified = {}
    record_summaries = []
    memberships_by_record: dict[str, list[Any]] = {}
    for membership in memberships:
        memberships_by_record.setdefault(str(membership.ccd_master), []).append(
            membership
        )
    record_evidence = []
    for record_id in record_ids:
        row = dict(record_rows[record_id])
        row["source"] = str(row.get("ccd_reg_source") or row.get("source") or "")
        seed_fingerprints[record_id] = identity_fingerprint(row, seed_policy)
        modified[record_id] = str(row.get("modified") or "")
        current_group_names = sorted(
            {
                str(membership.identity_group)
                for membership in memberships_by_record.get(record_id, [])
            }
        )
        record_summaries.append(
            {
                "record_id": record_id,
                "source": row["source"],
                "current_identity_groups": current_group_names,
            }
        )
        record_evidence.append(
            {
                "record_id": record_id,
                "source": row["source"],
                "current_identity_groups": current_group_names,
                "values": {
                    attribute: _display_evidence_value(
                        seed_policy.value(row, attribute), sensitive_values_visible
                    )
                    for attribute in evidence_attributes
                },
            }
        )

    decisions_by_name = {str(row.name): row for row in decisions}
    active_identity_groups = []
    for group in groups:
        members = sorted(
            str(membership.ccd_master)
            for membership in memberships
            if str(membership.identity_group) == str(group.name)
        )
        decision = decisions_by_name.get(str(group.originating_decision))
        group_summary = {
            "identity_group": str(group.name),
            "status": str(group.status),
            "originating_decision": str(group.originating_decision),
            "records": members,
        }
        if decision:
            group_summary.update(
                {
                    "decision_type": str(decision.decision_type or ""),
                    "decision_origin": str(decision.origin or ""),
                    "decision_origin_doctype": str(decision.origin_doctype or ""),
                    "decision_origin_document": str(decision.origin_document or ""),
                }
            )
        active_identity_groups.append(group_summary)
    active_exclusion_summaries = [
        {
            "exclusion": str(row.name),
            "left_record": str(row.left_record),
            "right_record": str(row.right_record),
            "originating_decision": str(row.originating_decision),
            "status": str(row.status),
        }
        for row in exclusions
    ]
    active_group_overlaps = []
    for scope in included:
        scope_records = set(str(value) for value in scope["records"])
        for group in active_identity_groups:
            shared_records = sorted(scope_records.intersection(group["records"]))
            if not shared_records:
                continue
            active_group_overlaps.append(
                {
                    "pending_doctype": str(scope["doctype"]),
                    "pending_document": str(scope["name"]),
                    "pending_origin": str(scope["origin"]),
                    "pending_result": str(scope["result"]),
                    "pending_records": sorted(scope_records),
                    "pending_recommendations": sorted(
                        str(value)
                        for value in scope.get("recommendation_ids", ())
                    ),
                    "identity_group": group["identity_group"],
                    "identity_group_records": group["records"],
                    "shared_records": shared_records,
                }
            )

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
    included_summaries = [_scope_summary(scope) for scope in included]
    included_snapshots = [
        {
            "key": scope["key"],
            "policy_snapshot_sha256": scope["policy_snapshot_sha256"],
            "expected_fingerprints": scope["expected_fingerprints"],
            "expected_modified": scope["expected_modified"],
            "recommendation_ids": scope.get("recommendation_ids", ()),
            "source_doctype": scope.get("source_doctype", ""),
            "source_document": scope.get("source_document", ""),
            "activation_batch": scope.get("activation_batch", ""),
        }
        for scope in included
    ]
    payload = {
        "seed": seed["key"],
        "records": [
            {
                "name": record_id,
                "modified": modified[record_id],
                "identity_fingerprint": seed_fingerprints[record_id],
            }
            for record_id in record_ids
        ],
        "included_pending_scopes": included_summaries,
        "included_pending_source_snapshots": included_snapshots,
        "memberships": _rows_as_dicts(memberships, membership_fields),
        "groups": _rows_as_dicts(groups, group_fields),
        "exclusions": _rows_as_dicts(exclusions, exclusion_fields),
        "decisions": _rows_as_dicts(decisions, decision_fields),
        "circuit_breaker": {
            "automation_paused": bool(settings.automation_paused),
            "pause_scope": str(settings.pause_scope or ""),
            "pause_reason": str(settings.pause_reason or ""),
        },
    }
    return {
        "seed": seed,
        "record_ids": record_ids,
        "record_rows": record_rows,
        "record_summaries": record_summaries,
        "record_evidence": record_evidence,
        "evidence_attributes": evidence_attributes,
        "sensitive_values_visible": sensitive_values_visible,
        "fingerprints": seed_fingerprints,
        "modified": modified,
        "memberships": memberships,
        "groups": groups,
        "exclusions": exclusions,
        "decisions": decisions,
        "current_groups": current_groups,
        "active_identity_groups": active_identity_groups,
        "active_exclusions": active_exclusion_summaries,
        "active_group_overlaps": active_group_overlaps,
        "prior_same_groups": tuple(prior_same_groups),
        "included": included,
        "included_summaries": included_summaries,
        "adjacent": adjacent,
        "adjacent_total": adjacent_total,
        "adjacent_truncated": adjacent_total > len(adjacent),
        "default_groups": default_groups,
        "different_constraints": tuple(sorted(set(different_constraints))),
        "constraint_conflicts": constraint_conflicts,
        "hard_conflicts": frozen_conflicts,
        "automation_paused": bool(settings.automation_paused),
        "pause_scope": str(settings.pause_scope or ""),
        "pause_reason": str(settings.pause_reason or ""),
        "scope_fingerprint": stable_payload_fingerprint(payload),
    }


def _group_index(groups: Iterable[Iterable[str]]) -> dict[str, int]:
    return {
        str(record_id): index
        for index, group in enumerate(groups)
        for record_id in group
    }


def _scope_satisfied(scope: dict[str, Any], groups: Iterable[Iterable[str]]) -> bool:
    normalized = tuple(tuple(str(item) for item in group) for group in groups)
    group_for = _group_index(normalized)
    for same_group in scope["groups"]:
        if len(same_group) > 1 and len({group_for[item] for item in same_group}) != 1:
            return False
    return all(group_for[left] != group_for[right] for left, right in scope["exclusions"])


def _decision_type(groups: Iterable[Iterable[str]]) -> str:
    normalized = tuple(tuple(group) for group in groups)
    if len(normalized) == 1:
        return "Same"
    if all(len(group) == 1 for group in normalized):
        return "Different"
    return "Partition"


def _preview(
    seed_doctype: str,
    seed_document: str,
    replacement_groups_json: Any | None = None,
) -> dict[str, Any]:
    context = _combined_context(seed_doctype, seed_document)
    if replacement_groups_json in (None, ""):
        replacement_groups = context["default_groups"]
    else:
        raw_groups = _load_list(replacement_groups_json, "Final identity partition")
        try:
            replacement_groups = normalize_partition(context["record_ids"], raw_groups)
        except ValueError as exc:
            frappe.throw(str(exc))
    replacement_exclusions = exclusions_for_partition(replacement_groups)
    current_exclusion_pairs = tuple(
        sorted(
            tuple(sorted((str(row.left_record), str(row.right_record))))
            for row in context["exclusions"]
        )
    )
    changed = (
        replacement_groups != context["current_groups"]
        or replacement_exclusions != current_exclusion_pairs
    )

    warnings: list[str] = []
    split_groups = partition_splits_groups(
        replacement_groups, context["prior_same_groups"]
    )
    if split_groups:
        warnings.append("splits_active_identity_group")
    contradicted_different = conflicting_different_pairs(
        replacement_groups, context["different_constraints"]
    )
    if contradicted_different:
        warnings.append("overrides_finalized_different_constraint")
    overridden_pending = [
        scope["key"]
        for scope in context["included"]
        if not _scope_satisfied(scope, replacement_groups)
    ]
    if overridden_pending:
        warnings.append("overrides_finalized_pending_decision")
    if len(context["groups"]) > 1:
        warnings.append("merges_multiple_active_identity_groups")
    if any(str(row.status) != "Active" for row in context["memberships"]):
        warnings.append("current_membership_needs_revalidation")
    if any(str(row.status) != "Active" for row in context["groups"]):
        warnings.append("current_group_needs_revalidation")

    records = {key: dict(value) for key, value in context["record_rows"].items()}
    for row in records.values():
        row["source"] = str(row.get("ccd_reg_source") or row.get("source") or "")
    policy = _policy(context["seed"]["policy_snapshot_json"])
    hkid_conflicts = complete_hkid_conflicts(replacement_groups, records, policy)
    if hkid_conflicts:
        warnings.append("complete_hkid_conflict_governance_override")
    same_source_groups = []
    for group in replacement_groups:
        counts = Counter(str(records[item].get("source") or "") for item in group)
        if any(count > 1 for source, count in counts.items() if source):
            same_source_groups.append(group)
    if same_source_groups:
        warnings.append("same_source_duplicates_governance_override")

    key = stable_payload_fingerprint(
        {
            "seed": context["seed"]["key"],
            "scope_fingerprint": context["scope_fingerprint"],
            "replacement_groups": replacement_groups,
        }
    )
    return {
        "zero_write": True,
        "eligible": not context["hard_conflicts"],
        "seed_doctype": context["seed"]["doctype"],
        "seed_document": context["seed"]["name"],
        "seed_origin": context["seed"]["origin"],
        "materialization_enabled": materialization_enabled(automated=False),
        "requires_materialization_enabled": changed,
        "scope_fingerprint": context["scope_fingerprint"],
        "resolution_key": key,
        "records": context["record_summaries"],
        "record_evidence": context["record_evidence"],
        "evidence_attributes": context["evidence_attributes"],
        "sensitive_values_visible": context["sensitive_values_visible"],
        "current_groups": context["current_groups"],
        "active_identity_groups": context["active_identity_groups"],
        "active_exclusions": context["active_exclusions"],
        "active_group_overlaps": context["active_group_overlaps"],
        "default_groups": context["default_groups"],
        "replacement_groups": replacement_groups,
        "replacement_exclusions": replacement_exclusions,
        "included_pending_scopes": context["included_summaries"],
        "adjacent_unreviewed_scopes": context["adjacent"],
        "adjacent_unreviewed_count": context["adjacent_total"],
        "adjacent_unreviewed_truncated": context["adjacent_truncated"],
        "hard_conflicts": context["hard_conflicts"],
        "automation_paused": context["automation_paused"],
        "pause_scope": context["pause_scope"],
        "pause_reason": context["pause_reason"],
        "constraint_conflicts": context["constraint_conflicts"],
        "warnings": sorted(set(warnings)),
        "requires_safety_confirmation": bool(warnings),
        "overridden_pending_scopes": overridden_pending,
        "already_represented": not changed,
        "changed": changed,
        "seed_requires_approval": (
            context["seed"]["doctype"] == ACTIVATION_ITEM_DOCTYPE
        ),
        "seed_approved": (
            context["seed"]["doctype"] != ACTIVATION_ITEM_DOCTYPE
            or context["seed"].get("activation_batch_status") == "Approved"
        ),
        "activation_batch": context["seed"].get("activation_batch", ""),
        "activation_batch_status": context["seed"].get(
            "activation_batch_status", ""
        ),
        "planned": {
            "replacement_decision_type": _decision_type(replacement_groups),
            "ended_groups": len(context["groups"]) if changed else 0,
            "ended_memberships": len(context["memberships"]) if changed else 0,
            "superseded_decisions": (
                sum(1 for row in context["decisions"] if str(row.status) == "Active")
                if changed
                else 0
            ),
            "superseded_exclusions": len(context["exclusions"]) if changed else 0,
            "new_groups": sum(1 for group in replacement_groups if len(group) > 1) if changed else 0,
            "new_memberships": sum(len(group) for group in replacement_groups if len(group) > 1) if changed else 0,
            "new_exclusions": len(replacement_exclusions) if changed else 0,
            "physical_ccd_master_merges": 0,
        },
        "_context": context,
    }


def _public(preview: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in preview.items() if key != "_context"}


@frappe.whitelist()
def get_combined_component_context(
    seed_doctype: str, seed_document: str
) -> dict[str, Any]:
    """Return the authoritative combined scope and adjacent evidence without writes."""
    _require_manager()
    return _public(_preview(seed_doctype, seed_document))


@frappe.whitelist()
def preview_combined_component_resolution(
    seed_doctype: str,
    seed_document: str,
    replacement_groups_json: Any,
) -> dict[str, Any]:
    """Preview an exact final partition for the recursively expanded scope."""
    _require_manager()
    return _public(
        _preview(seed_doctype, seed_document, replacement_groups_json)
    )


def _set_existing_fields(doctype: str, name: str, values: dict[str, Any]) -> None:
    fieldnames = {field.fieldname for field in frappe.get_meta(doctype).fields}
    safe_values = {key: value for key, value in values.items() if key in fieldnames}
    if safe_values:
        frappe.db.set_value(doctype, name, safe_values, update_modified=False)


def _active_group_names_for_records(record_ids: Iterable[str]) -> list[str]:
    ids = tuple(sorted({str(item) for item in record_ids}))
    if not ids:
        return []
    return sorted(
        {
            str(value)
            for value in frappe.get_all(
                MEMBERSHIP_DOCTYPE,
                filters={
                    "ccd_master": ["in", ids],
                    "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
                },
                pluck="identity_group",
                limit_page_length=100_000,
            )
        }
    )


def _lock_frozen_source_documents(context: dict[str, Any]) -> None:
    """Lock every mutable source row used to recompute the frozen scope."""
    by_doctype: dict[str, set[str]] = {}

    def include(doctype: str, names: Iterable[str]) -> None:
        clean = {str(name) for name in names if str(name)}
        if clean:
            by_doctype.setdefault(str(doctype), set()).update(clean)

    for scope in context["included"]:
        include(scope["doctype"], (scope["name"],))
        include(scope.get("source_doctype", ""), (scope.get("source_document", ""),))
        include(RECOMMENDATION_DOCTYPE, scope.get("recommendation_ids", ()))
        include(ACTIVATION_BATCH_DOCTYPE, (scope.get("activation_batch", ""),))
    for adjacent in context["adjacent"]:
        include(adjacent["doctype"], adjacent.get("documents", ()))

    for doctype in sorted(by_doctype):
        _lock_named_rows(doctype, sorted(by_doctype[doctype]))


def _mark_pending_sources(
    included: Iterable[dict[str, Any]],
    replacement_groups: tuple[tuple[str, ...], ...],
    replacement_decision: str,
    resolution_name: str,
    reason: str,
) -> set[str]:
    affected_canaries: set[str] = set()
    for scope in included:
        satisfied = _scope_satisfied(scope, replacement_groups)
        identity_groups = _active_group_names_for_records(scope["records"])
        decision_value = replacement_decision or None
        outcome_note = f"overlap_resolution:{resolution_name}"
        if scope["doctype"] == CANDIDATE_DOCTYPE:
            _set_existing_fields(
                CANDIDATE_DOCTYPE,
                scope["name"],
                {
                    "materialization_status": "Applied" if satisfied else "Superseded",
                    "identity_decision": decision_value if satisfied else None,
                    "identity_groups_json": _json(identity_groups) if satisfied else _json([]),
                    "materialization_error": outcome_note,
                    "correction_decision": decision_value if not satisfied else None,
                    "reversed_at": frappe.utils.now_datetime() if not satisfied else None,
                    "reversed_by": frappe.session.user if not satisfied else None,
                    "reversal_reason": reason if not satisfied else None,
                },
            )
        elif scope["doctype"] == COMPONENT_DOCTYPE:
            _set_existing_fields(
                COMPONENT_DOCTYPE,
                scope["name"],
                {
                    "materialization_status": "Applied" if satisfied else "Corrected",
                    "identity_decision": decision_value if satisfied else None,
                    "identity_groups_json": _json(identity_groups) if satisfied else _json([]),
                    "materialization_error": outcome_note,
                    "correction_decision": decision_value if not satisfied else None,
                    "corrected_at": frappe.utils.now_datetime() if not satisfied else None,
                    "corrected_by": frappe.session.user if not satisfied else None,
                    "correction_reason": reason if not satisfied else None,
                },
            )
        elif scope["doctype"] == ACTIVATION_ITEM_DOCTYPE:
            item = frappe.get_doc(ACTIVATION_ITEM_DOCTYPE, scope["name"])
            group_value = identity_groups[0] if satisfied and len(identity_groups) == 1 else None
            for recommendation_name in scope["recommendation_ids"]:
                recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
                if satisfied:
                    _change_recommendation_status(
                        recommendation,
                        "Approved",
                        "Approved",
                        outcome_note,
                        approved=True,
                    )
                else:
                    _change_recommendation_status(
                        recommendation,
                        "Superseded",
                        "Superseded",
                        reason,
                    )
                frappe.db.set_value(
                    RECOMMENDATION_DOCTYPE,
                    recommendation.name,
                    {
                        "rollout_state": "Applied",
                        "activation_batch": item.parent,
                        "identity_decision": decision_value,
                        "identity_group": group_value,
                    },
                    update_modified=False,
                )
                affected_canaries.add(str(recommendation.canary_run))
            _set_existing_fields(
                ACTIVATION_ITEM_DOCTYPE,
                item.name,
                {
                    "status": "Applied" if satisfied else "Corrected",
                    "identity_decision": decision_value,
                    "identity_group": group_value,
                    "error_code": outcome_note,
                },
            )
            terminal = {"Applied", "Already Applied", "Corrected"}
            statuses = set(
                str(value)
                for value in frappe.get_all(
                    ACTIVATION_ITEM_DOCTYPE,
                    filters={"parent": item.parent},
                    pluck="status",
                    limit_page_length=100_000,
                )
            )
            if statuses and statuses <= terminal:
                _set_existing_fields(
                    ACTIVATION_BATCH_DOCTYPE,
                    item.parent,
                    {
                        "status": "Applied",
                        "applied_at": frappe.utils.now_datetime(),
                        "applied_by": frappe.session.user,
                    },
                )
    for canary_name in affected_canaries:
        _refresh_run_counts(canary_name)
    return affected_canaries


def _mark_prior_origins_superseded(
    decisions: Iterable[Any],
    resolution_name: str,
    replacement_decision: str,
    reason: str,
    now: Any,
) -> None:
    ordinary_origins = [
        decision
        for decision in decisions
        if str(decision.origin) != "Governance Override"
    ]
    _mark_origin_corrected(
        ordinary_origins,
        resolution_name,
        replacement_decision,
        reason,
        now,
    )
    for decision in decisions:
        if str(decision.origin) != "Governance Override":
            continue
        origin_doctype = str(decision.origin_doctype or "")
        origin_document = str(decision.origin_document or "")
        if origin_doctype == "CCD Identity Correction" and frappe.db.exists(
            origin_doctype, origin_document
        ):
            # The Correction's fixed superseded_by Link accepts only another
            # Correction. The Decision chain and new Resolution carry the
            # cross-DocType link, so only close the old audit here.
            _set_existing_fields(origin_doctype, origin_document, {"status": "Superseded"})
        elif origin_doctype == RESOLUTION_DOCTYPE and frappe.db.exists(
            origin_doctype, origin_document
        ):
            _set_existing_fields(
                origin_doctype,
                origin_document,
                {
                    "status": "Superseded",
                    "superseded_by_identity_decision": replacement_decision,
                },
            )


def _create_resolution_doc(
    preview: dict[str, Any], reason: str, demonstration: bool
) -> Any:
    context = preview["_context"]
    membership_fields = (
        "name",
        "ccd_master",
        "identity_group",
        "identity_fingerprint",
        "status",
        "originating_decision",
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
    return frappe.get_doc(
        {
            "doctype": RESOLUTION_DOCTYPE,
            "resolution_key": preview["resolution_key"],
            "status": "Applying",
            "seed_doctype": preview["seed_doctype"],
            "seed_document": preview["seed_document"],
            "seed_origin": preview["seed_origin"],
            "scope_fingerprint": preview["scope_fingerprint"],
            "participant_records_json": _json(context["record_ids"]),
            "included_pending_scopes_json": _json(context["included_summaries"]),
            "adjacent_unreviewed_scopes_json": _json(context["adjacent"]),
            "prior_identity_decisions_json": _json([row.name for row in context["decisions"]]),
            "prior_identity_groups_json": _json([row.name for row in context["groups"]]),
            "prior_memberships_json": _json(
                _rows_as_dicts(context["memberships"], membership_fields)
            ),
            "prior_exclusions_json": _json(
                _rows_as_dicts(context["exclusions"], exclusion_fields)
            ),
            "replacement_groups_json": _json(preview["replacement_groups"]),
            "replacement_exclusions_json": _json(preview["replacement_exclusions"]),
            "resolution_reason": reason,
            "warnings_json": _json(preview["warnings"]),
            "is_demonstration": int(demonstration),
            "applied_at": frappe.utils.now_datetime(),
            "applied_by": frappe.session.user,
            "ended_group_count": preview["planned"]["ended_groups"],
            "ended_membership_count": preview["planned"]["ended_memberships"],
            "superseded_exclusion_count": preview["planned"]["superseded_exclusions"],
        }
    ).insert(ignore_permissions=True)


@frappe.whitelist()
def apply_combined_component_resolution(
    seed_doctype: str,
    seed_document: str,
    replacement_groups_json: Any,
    expected_scope_fingerprint: str,
    reason: str,
    confirm_seed_document: str,
    confirm_safety_warnings: int | str = 0,
    is_demonstration: int | str = 0,
) -> dict[str, Any]:
    """Atomically apply one frozen, recursively expanded identity partition."""
    _require_manager()
    seed_doctype = str(seed_doctype or "").strip()
    seed_document = str(seed_document or "").strip()
    expected_scope_fingerprint = str(expected_scope_fingerprint or "").strip()
    reason = str(reason or "").strip()
    if seed_doctype not in SUPPORTED_SEED_DOCTYPES:
        frappe.throw("Unsupported overlap-resolution seed DocType")
    if not seed_document or str(confirm_seed_document or "").strip() != seed_document:
        frappe.throw("Type the exact seed document ID to confirm this resolution")
    if not expected_scope_fingerprint:
        frappe.throw("A fresh zero-write combined-component preview is required")
    if not reason:
        frappe.throw("A resolution reason is required")
    demonstration = _as_bool(is_demonstration)
    _require_activation_seed_approved_for_apply(seed_doctype, seed_document)

    submitted_groups = _load_list(replacement_groups_json, "Final identity partition")
    preliminary = _preview(seed_doctype, seed_document, submitted_groups)
    if preliminary["scope_fingerprint"] != expected_scope_fingerprint:
        frappe.throw("The combined identity scope changed after preview; preview it again")
    if preliminary["hard_conflicts"]:
        frappe.throw("Combined identity safety checks failed: " + ", ".join(preliminary["hard_conflicts"]))
    if preliminary["requires_safety_confirmation"] and not _as_bool(confirm_safety_warnings):
        frappe.throw("Explicitly confirm every safety warning shown by the preview")
    if preliminary["changed"] and not materialization_enabled(automated=False):
        frappe.throw("Enable Live Identity Materialization, then run the combined preview again")

    existing = frappe.db.get_value(
        RESOLUTION_DOCTYPE,
        {"resolution_key": preliminary["resolution_key"], "status": ["in", ["Applied", "No Change"]]},
        ["name", "status", "replacement_identity_decision"],
        as_dict=True,
    )
    if existing:
        return {
            "status": "Already Applied",
            "resolution": existing.name,
            "identity_decision": existing.replacement_identity_decision or "",
        }

    try:
        context = preliminary["_context"]
        _lock_records(context["record_ids"])
        _lock_named_rows(DECISION_DOCTYPE, [row.name for row in context["decisions"]])
        _lock_named_rows(GROUP_DOCTYPE, [row.name for row in context["groups"]])
        _lock_named_rows(MEMBERSHIP_DOCTYPE, [row.name for row in context["memberships"]])
        _lock_named_rows(EXCLUSION_DOCTYPE, [row.name for row in context["exclusions"]])
        _lock_frozen_source_documents(context)

        locked = _preview(seed_doctype, seed_document, submitted_groups)
        if (
            locked["scope_fingerprint"] != expected_scope_fingerprint
            or locked["resolution_key"] != preliminary["resolution_key"]
        ):
            frappe.throw("The combined identity scope changed while locking; preview it again")
        if locked["hard_conflicts"]:
            frappe.throw("Combined identity safety checks failed: " + ", ".join(locked["hard_conflicts"]))
        context = locked["_context"]
        resolution = _create_resolution_doc(locked, reason, demonstration)
        now = frappe.utils.now_datetime()

        if not locked["changed"]:
            _mark_pending_sources(
                context["included"],
                locked["replacement_groups"],
                "",
                resolution.name,
                reason,
            )
            frappe.db.set_value(
                RESOLUTION_DOCTYPE,
                resolution.name,
                {"status": "No Change"},
                update_modified=False,
            )
            _append_event(
                entity_doctype=RESOLUTION_DOCTYPE,
                entity_name=resolution.name,
                event_type="Activate",
                reason=reason,
                nonce=f"overlap_no_change:{resolution.name}",
                from_status="Applying",
                to_status="No Change",
                metadata={"already_represented": True},
                is_demonstration=demonstration,
            )
            frappe.db.commit()
            return {
                "status": "No Change",
                "resolution": resolution.name,
                "already_represented": True,
                "identity_decision": "",
                "created_groups": 0,
                "created_memberships": 0,
                "created_exclusions": 0,
            }

        event_nonce = f"identity_overlap_resolution:{resolution.name}:{now}"
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
                metadata={"overlap_resolution": resolution.name},
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
                for replacement_group in locked["replacement_groups"]
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
                metadata={"overlap_resolution": resolution.name},
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
                metadata={"overlap_resolution": resolution.name},
                is_demonstration=demonstration,
            )

        seed = context["seed"]
        result = materialize_identity(
            origin="Governance Override",
            origin_doctype=RESOLUTION_DOCTYPE,
            origin_document=resolution.name,
            policy_snapshot_json=seed["policy_snapshot_json"],
            policy_snapshot_sha256=seed["policy_snapshot_sha256"],
            matching_policy=seed["matching_policy"],
            record_ids=context["record_ids"],
            groups=locked["replacement_groups"],
            exclusions=locked["replacement_exclusions"],
            expected_fingerprints=context["fingerprints"],
            expected_modified=context["modified"],
            reason_codes=["manager_complete_overlap_resolution"],
            review_context={
                "overlap_resolution": resolution.name,
                "seed_doctype": seed_doctype,
                "seed_document": seed_document,
                "included_pending_scopes": context["included_summaries"],
                "resolution_reason": reason,
                "accepted_warnings": locked["warnings"],
            },
            governance_override=bool(locked["warnings"]),
            governance_notes=reason if locked["warnings"] else "",
            is_demonstration=demonstration,
            require_enabled=True,
        )
        replacement_decision = str(result["identity_decision"])
        active_prior_decisions = [
            row for row in context["decisions"] if str(row.status) == "Active"
        ]
        max_version = max(
            [int(row.decision_version or 1) for row in context["decisions"]] or [0]
        )
        frappe.db.set_value(
            DECISION_DOCTYPE,
            replacement_decision,
            {"decision_version": max_version + 1},
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
                metadata={"overlap_resolution": resolution.name},
                is_demonstration=demonstration,
            )
        _link_replacement_groups(
            context["groups"], context["memberships"], replacement_decision
        )
        _mark_prior_origins_superseded(
            active_prior_decisions,
            resolution.name,
            replacement_decision,
            reason,
            now,
        )
        _mark_pending_sources(
            context["included"],
            locked["replacement_groups"],
            replacement_decision,
            resolution.name,
            reason,
        )
        frappe.db.set_value(
            RESOLUTION_DOCTYPE,
            resolution.name,
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
            entity_doctype=RESOLUTION_DOCTYPE,
            entity_name=resolution.name,
            event_type="Activate",
            reason=reason,
            nonce=event_nonce,
            from_status="Applying",
            to_status="Applied",
            identity_decision=replacement_decision,
            metadata={
                "seed_doctype": seed_doctype,
                "seed_document": seed_document,
                "replacement_groups": locked["replacement_groups"],
            },
            is_demonstration=demonstration,
        )
        frappe.db.commit()
        return {
            "status": "Applied",
            "resolution": resolution.name,
            "identity_decision": replacement_decision,
            "replacement_decision_type": locked["planned"]["replacement_decision_type"],
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
