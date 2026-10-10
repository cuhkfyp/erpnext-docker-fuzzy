"""Prospective validation and guarded unattended Splink materialization.

All heavy apply paths are background jobs.  The public preview actions may
create only audit/planning documents; they never write identity state.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from collections import Counter
from typing import Any, Iterable

import frappe
from frappe.utils import add_days, now_datetime

from db_connector.api_identity_resolution import (
    _append_event,
    _current_memberships,
    materialization_enabled,
    materialize_identity,
    preview_materialization,
)
from db_connector.fuzzy_matching.automation import current_shared_group, rolling_qc_summary
from db_connector.fuzzy_matching.identity import expected_identity_fingerprints
from db_connector.fuzzy_matching.splink_automation import (
    FROZEN_AUTOMATIC_CUTOFF,
    HOLDOUT_SIZE,
    HOLDOUT_SOURCE_ALLOCATION,
    MIN_ELIGIBLE_HOLDOUT,
    VALIDATION_SIZE,
    VALIDATION_SOURCE_ALLOCATION,
    WEEKLY_PAIR_CAPACITY,
    allocate_shared_capacity,
    automatic_components,
    canonical_source_pair,
    component_metadata,
    provenance_fingerprint,
    select_stratified_cohorts,
    validation_gate,
)

RUN_DOCTYPE = "CCD Match Evaluation Run"
PAIR_DOCTYPE = "CCD Match Evaluation Pair"
QUEUE_DOCTYPE = "CCD Match Review Queue Run"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
BATCH_DOCTYPE = "CCD Splink Automation Batch"
ITEM_DOCTYPE = "CCD Splink Automation Batch Item"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
GROUP_DOCTYPE = "CCD Identity Group"
FINAL_STATUSES = {"Agreed", "Adjudicated"}
EXPECTED_ULTRA_HIGH_EDGES = 402
EXPECTED_ISOLATED_PAIRS = 223
EXPECTED_PREVIOUSLY_REVIEWED = 8


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_reviewer() -> None:
    if not (
        {"System Manager", "CCD Match Reviewer", "CCD Match Sensitive Reviewer"}
        & set(frappe.get_roles())
    ):
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _lock_rows(doctype: str, names: Iterable[str]) -> None:
    ordered = tuple(sorted({str(name) for name in names if str(name)}))
    if not ordered:
        return
    placeholders = ", ".join(["%s"] * len(ordered))
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name IN ({placeholders}) "
        "ORDER BY name FOR UPDATE",
        ordered,
    )


def _lock_settings() -> None:
    frappe.db.sql(
        "SELECT field FROM `tabSingles` WHERE doctype = %s ORDER BY field FOR UPDATE",
        (SETTINGS_DOCTYPE,),
    )


def _set_settings(values: dict[str, Any]) -> None:
    for fieldname, value in values.items():
        frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)


def _queue_rows(queue_run: str, *, minimum_score: float) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in frappe.get_all(
            CANDIDATE_DOCTYPE,
            filters={
                "queue_run": queue_run,
                "probabilistic_score": [">=", float(minimum_score)],
            },
            fields=[
                "name",
                "pair_key",
                "pair_fingerprint",
                "left_record",
                "right_record",
                "left_source",
                "right_source",
                "left_modified_at",
                "right_modified_at",
                "left_identity_fingerprint",
                "right_identity_fingerprint",
                "source_pair",
                "blocking_routes",
                "probabilistic_score",
                "review_threshold",
                "priority_rank",
                "review_status",
                "final_label",
                "stale",
                "automation_reserved",
                "automation_status",
                "automation_batch",
            ],
            order_by="priority_rank, name",
            limit_page_length=100_000,
        )
    ]


def _reviewed_candidate_names(rows: Iterable[dict[str, Any]]) -> set[str]:
    rows = list(rows)
    names = {str(row["name"]) for row in rows if row.get("final_label")}
    candidates = [str(row["name"]) for row in rows]
    if candidates:
        names.update(
            str(name)
            for name in frappe.get_all(
                "CCD Match Review Label",
                filters={
                    "parenttype": CANDIDATE_DOCTYPE,
                    "parent": ["in", candidates],
                },
                pluck="parent",
                limit_page_length=100_000,
            )
        )
    return names


def _source_scope_from_snapshot(policy: dict[str, Any]) -> str:
    """Return the canonical governed source scope for either snapshot shape."""
    profiles = policy.get("source_profiles") or {}
    if isinstance(profiles, dict):
        sources = sorted(str(source) for source in profiles if source)
    else:
        sources = sorted(
            str(item.get("source") or "")
            for item in profiles
            if isinstance(item, dict) and item.get("source")
        )
    return _json(sources)


def _source_scope(queue: Any) -> str:
    try:
        policy = json.loads(queue.policy_snapshot_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        policy = {}
    return _source_scope_from_snapshot(policy)


def _runtime_versions(queue: Any) -> str:
    summary = json.loads(queue.summary_json or "{}")
    return _json(
        {
            "splink_adapter_version": str(queue.splink_adapter_version or ""),
            "splink_dependencies": summary.get("splink_dependencies") or {},
            "blocking_version": summary.get("blocking_version") or "",
            "random_match_prior": summary.get("random_match_prior"),
            "u_random_seed": summary.get("frozen_u_random_seed"),
        }
    )


def _provenance_values(queue: Any, *, cutoff: float) -> dict[str, Any]:
    evaluation = frappe.get_doc(RUN_DOCTYPE, queue.threshold_evaluation_run)
    return {
        "queue_run": str(queue.name),
        "canary_run": str(queue.canary_run),
        "policy_snapshot_sha256": str(queue.policy_snapshot_sha256),
        "model_versions_json": str(evaluation.model_versions_json or "{}"),
        "runtime_versions_json": _runtime_versions(queue),
        "source_scope_json": _source_scope(queue),
        "frozen_cutoff": f"{float(cutoff):.12f}",
    }


def _authorization_current(run: Any) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if run.run_purpose != "Splink Automatic Validation":
        reasons.append("wrong_run_purpose")
        return False, reasons
    if not run.validation_queue_run:
        reasons.append("validation_queue_missing")
        return False, reasons
    queue = frappe.get_doc(QUEUE_DOCTYPE, run.validation_queue_run)
    if queue.status != "Ready":
        reasons.append(f"queue_not_ready:{queue.status}")
    current = _provenance_values(queue, cutoff=float(run.frozen_automatic_cutoff or 0))
    current_fingerprint = provenance_fingerprint(current)
    if current_fingerprint != str(run.authorization_fingerprint or ""):
        reasons.append("frozen_provenance_changed")
    if str(queue.canary_run) != str(run.validation_canary_run):
        reasons.append("canary_changed")
    if abs(float(run.frozen_automatic_cutoff or 0) - FROZEN_AUTOMATIC_CUTOFF) > 1e-12:
        reasons.append("frozen_cutoff_changed")
    try:
        from db_connector.api_fuzzy_canary import (
            _policy_from_doc,
            _policy_snapshot,
            _snapshot_hash,
        )
        from db_connector.fuzzy_matching.blocking import BLOCKING_VERSION
        from db_connector.fuzzy_matching.splink_adapter import (
            RANDOM_MATCH_PRIOR,
            SPLINK_ADAPTER_VERSION,
            dependency_versions,
        )

        current_policy_snapshot = _policy_snapshot(
            _policy_from_doc(frappe.get_doc("CCD Matching Policy", run.matching_policy))
        )
        if _snapshot_hash(current_policy_snapshot) != str(queue.policy_snapshot_sha256):
            reasons.append("policy_or_source_mapping_changed")
        current_source_scope = _source_scope_from_snapshot(current_policy_snapshot)
        if current_source_scope != str(run.source_scope_json or ""):
            reasons.append("source_scope_changed")
        frozen_runtime = json.loads(run.runtime_versions_json or "{}")
        if frozen_runtime.get("splink_adapter_version") != SPLINK_ADAPTER_VERSION:
            reasons.append("splink_adapter_changed")
        if frozen_runtime.get("splink_dependencies") != dependency_versions():
            reasons.append("splink_dependencies_changed")
        if frozen_runtime.get("blocking_version") != BLOCKING_VERSION:
            reasons.append("blocking_runtime_changed")
        if abs(
            float(frozen_runtime.get("random_match_prior") or 0)
            - float(RANDOM_MATCH_PRIOR)
        ) > 1e-15:
            reasons.append("splink_prior_changed")
    except Exception as exc:
        reasons.append(f"current_provenance_unavailable:{type(exc).__name__}")
    return not reasons, reasons


def _mark_validation_stale(run: Any, reasons: list[str]) -> None:
    if not reasons:
        return
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            "authorization_stale": 1,
            "authorization_stale_reason": ",".join(sorted(set(reasons)))[:140],
        },
        update_modified=False,
    )
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if str(settings.authorized_splink_validation_run or "") == str(run.name):
        _pause_splink("authorization_invalidated:" + ",".join(reasons))


def _validation_inventory(queue: Any) -> dict[str, Any]:
    rows = _queue_rows(queue.name, minimum_score=FROZEN_AUTOMATIC_CUTOFF)
    metadata = component_metadata(rows)
    reviewed = _reviewed_candidate_names(rows)
    isolated = [row for row in rows if metadata[str(row["name"])]["isolated_pair"]]
    reviewed_isolated = [row for row in isolated if str(row["name"]) in reviewed]
    eligible = [row for row in isolated if str(row["name"]) not in reviewed]
    available_by_source = dict(
        sorted(Counter(canonical_source_pair(row["source_pair"]) for row in eligible).items())
    )
    required_by_source = {
        source_pair: int(VALIDATION_SOURCE_ALLOCATION.get(source_pair, 0))
        + int(HOLDOUT_SOURCE_ALLOCATION.get(source_pair, 0))
        for source_pair in sorted(
            set(VALIDATION_SOURCE_ALLOCATION) | set(HOLDOUT_SOURCE_ALLOCATION)
        )
    }
    return {
        "rows": rows,
        "metadata": metadata,
        "reviewed": reviewed,
        "isolated": isolated,
        "reviewed_isolated": reviewed_isolated,
        "eligible": eligible,
        "available_by_source": available_by_source,
        "required_by_source": required_by_source,
    }


@frappe.whitelist()
def preview_splink_automatic_validation(queue_run: str) -> dict[str, Any]:
    """Read-only aggregate inventory for the frozen 165/50 cohort proposal."""
    _require_manager()
    queue = frappe.get_doc(QUEUE_DOCTYPE, str(queue_run or "").strip())
    inventory = _validation_inventory(queue)
    available = inventory["available_by_source"]
    required = inventory["required_by_source"]
    actual = {
        "ultra_high_edges": len(inventory["rows"]),
        "isolated_pairs": len(inventory["isolated"]),
        "previously_reviewed_isolated": len(inventory["reviewed_isolated"]),
        "eligible_isolated_pairs": len(inventory["eligible"]),
    }
    checks = {
        "queue_ready": queue.status == "Ready",
        "ultra_high_edges": actual["ultra_high_edges"] == EXPECTED_ULTRA_HIGH_EDGES,
        "isolated_pairs": actual["isolated_pairs"] == EXPECTED_ISOLATED_PAIRS,
        "previously_reviewed_isolated": actual["previously_reviewed_isolated"]
        == EXPECTED_PREVIOUSLY_REVIEWED,
        "eligible_isolated_pairs": actual["eligible_isolated_pairs"]
        == VALIDATION_SIZE + HOLDOUT_SIZE,
        "source_allocations": all(
            int(available.get(source_pair, 0)) >= count
            for source_pair, count in required.items()
        ),
    }
    return {
        "queue_run": queue.name,
        "cutoff": FROZEN_AUTOMATIC_CUTOFF,
        "expected": {
            "ultra_high_edges": EXPECTED_ULTRA_HIGH_EDGES,
            "isolated_pairs": EXPECTED_ISOLATED_PAIRS,
            "previously_reviewed_isolated": EXPECTED_PREVIOUSLY_REVIEWED,
            "eligible_isolated_pairs": VALIDATION_SIZE + HOLDOUT_SIZE,
        },
        "actual": actual,
        "eligible_by_source": available,
        "required_by_source": required,
        "checks": checks,
        "ready_to_create": all(checks.values()),
        "identity_writes": 0,
    }


@frappe.whitelist(methods=["POST"])
def create_splink_automatic_validation(
    queue_run: str, deterministic_seed: str, confirm_queue_run: str
) -> dict[str, Any]:
    """Reserve the exact 165/50 blinded cohorts and create validation pairs."""
    _require_manager()
    queue_run = str(queue_run or "").strip()
    if str(confirm_queue_run or "").strip() != queue_run:
        frappe.throw("Type the exact Ready Review Queue ID to confirm cohort creation")
    seed = str(deterministic_seed or "").strip()
    if not seed:
        frappe.throw("A deterministic source-stratified random seed is required")
    _lock_rows(QUEUE_DOCTYPE, (queue_run,))
    queue = frappe.get_doc(QUEUE_DOCTYPE, queue_run)
    if queue.status != "Ready":
        frappe.throw("Only a Ready Splink Review Queue can create validation cohorts")
    if frappe.db.exists(
        RUN_DOCTYPE,
        {"run_purpose": "Splink Automatic Validation", "validation_queue_run": queue.name},
    ):
        frappe.throw("This Review Queue already has a Splink automatic validation")

    inventory = _validation_inventory(queue)
    rows = inventory["rows"]
    metadata = inventory["metadata"]
    isolated = inventory["isolated"]
    if len(rows) != EXPECTED_ULTRA_HIGH_EDGES:
        frappe.throw(
            f"Frozen queue mismatch: expected {EXPECTED_ULTRA_HIGH_EDGES} ultra-high edges, found {len(rows)}"
        )
    if len(isolated) != EXPECTED_ISOLATED_PAIRS:
        frappe.throw(
            f"Frozen queue mismatch: expected {EXPECTED_ISOLATED_PAIRS} isolated pairs, found {len(isolated)}"
        )
    reviewed_isolated = inventory["reviewed_isolated"]
    if len(reviewed_isolated) != EXPECTED_PREVIOUSLY_REVIEWED:
        frappe.throw(
            "Frozen queue mismatch: expected "
            f"{EXPECTED_PREVIOUSLY_REVIEWED} previously reviewed isolated pairs, "
            f"found {len(reviewed_isolated)}"
        )
    eligible = inventory["eligible"]
    if any(row.get("automation_reserved") for row in eligible):
        frappe.throw("One or more eligible candidates are already reserved")
    try:
        cohorts = select_stratified_cohorts(eligible, seed=seed)
    except ValueError as exc:
        frappe.throw(str(exc))
    if len(cohorts["validation"]) != VALIDATION_SIZE or len(cohorts["holdout"]) != HOLDOUT_SIZE:
        frappe.throw("The deterministic selection did not produce the frozen 165/50 cohorts")

    provenance = _provenance_values(queue, cutoff=FROZEN_AUTOMATIC_CUTOFF)
    fingerprint = provenance_fingerprint(provenance)
    source_allocation = {
        "validation": dict(Counter(canonical_source_pair(row["source_pair"]) for row in eligible if row["name"] in cohorts["validation"])),
        "holdout": dict(Counter(canonical_source_pair(row["source_pair"]) for row in eligible if row["name"] in cohorts["holdout"])),
    }
    run = frappe.get_doc(
        {
            "doctype": RUN_DOCTYPE,
            "matching_policy": queue.matching_policy,
            "policy_version": queue.policy_version,
            "run_purpose": "Splink Automatic Validation",
            "policy_snapshot_json": queue.policy_snapshot_json,
            "status": "Reviewing",
            "approval_status": "Pending Management Review",
            "snapshot_at": queue.snapshot_at,
            "sample_size": VALIDATION_SIZE,
            "double_review_count": VALIDATION_SIZE,
            "record_count": len({record for row in eligible for record in (row["left_record"], row["right_record"])}),
            "candidate_count": len(rows),
            "candidate_truncated": 0,
            "sampled_pair_count": VALIDATION_SIZE,
            "model_versions_json": provenance["model_versions_json"],
            "validation_queue_run": queue.name,
            "validation_canary_run": queue.canary_run,
            "frozen_automatic_cutoff": FROZEN_AUTOMATIC_CUTOFF,
            "validation_seed": seed,
            "source_allocation_json": _json(source_allocation),
            "source_scope_json": provenance["source_scope_json"],
            "runtime_versions_json": provenance["runtime_versions_json"],
            "authorization_fingerprint": fingerprint,
            "validation_target_count": VALIDATION_SIZE,
            "holdout_target_count": HOLDOUT_SIZE,
            "second_management_approval_status": "Not Requested",
        }
    ).insert(ignore_permissions=True)
    by_name = {str(row["name"]): row for row in eligible}
    now = now_datetime()
    for candidate_name in cohorts["validation"]:
        row = by_name[candidate_name]
        frappe.get_doc(
            {
                "doctype": PAIR_DOCTYPE,
                "evaluation_run": run.name,
                "review_candidate": candidate_name,
                "automation_cohort": "Validation",
                "source_pair": row["source_pair"],
                "left_record": row["left_record"],
                "right_record": row["right_record"],
                "left_source": row["left_source"],
                "right_source": row["right_source"],
                "left_modified_at": row["left_modified_at"],
                "right_modified_at": row["right_modified_at"],
                "blocking_routes": row["blocking_routes"],
                "probabilistic_score": row["probabilistic_score"],
                "probabilistic_available": 1,
                "needs_double_review": 1,
                "double_review_reason": "splink_automatic_validation",
                "review_status": "Unreviewed",
            }
        ).insert(ignore_permissions=True)
    for cohort, names in (
        ("Validation", cohorts["validation"]),
        ("Blinded Rollout Holdout", cohorts["holdout"]),
    ):
        for candidate_name in names:
            meta = metadata[candidate_name]
            frappe.db.set_value(
                CANDIDATE_DOCTYPE,
                candidate_name,
                {
                    "automation_validation_run": run.name,
                    "automation_cohort": cohort,
                    "automation_reserved": 1,
                    "automation_component_fingerprint": meta["component_fingerprint"],
                    "automation_component_size": meta["component_size"],
                    "automation_complete_clique": int(meta["complete_clique"]),
                    "automation_status": "Reserved Validation" if cohort == "Validation" else "Reserved Holdout",
                    "assigned_review_batch": None,
                    "assigned_to": None,
                    "assigned_at": None,
                    "due_at": None,
                },
                update_modified=False,
            )
    _append_event(
        entity_doctype=RUN_DOCTYPE,
        entity_name=run.name,
        event_type="Create",
        reason="splink_automatic_validation_cohorts_frozen",
        nonce=fingerprint,
        to_status="Reviewing",
        metadata={
            "queue_run": queue.name,
            "seed": seed,
            "cutoff": FROZEN_AUTOMATIC_CUTOFF,
            "ultra_high_edges": len(rows),
            "isolated_pairs": len(isolated),
            "previously_reviewed_excluded": len(reviewed_isolated),
            "validation": VALIDATION_SIZE,
            "holdout": HOLDOUT_SIZE,
            "source_allocation": source_allocation,
        },
    )
    frappe.db.commit()
    return {
        "validation_run": run.name,
        "status": run.status,
        "ultra_high_edges": len(rows),
        "isolated_pairs": len(isolated),
        "previously_reviewed_excluded": len(reviewed_isolated),
        "validation_pairs": VALIDATION_SIZE,
        "blinded_holdout_pairs": HOLDOUT_SIZE,
        "source_allocation": source_allocation,
        "authorization_fingerprint": fingerprint,
    }


def _validation_rows(run_name: str) -> list[Any]:
    return frappe.get_all(
        PAIR_DOCTYPE,
        filters={"evaluation_run": run_name, "automation_cohort": "Validation"},
        fields=[
            "name",
            "review_candidate",
            "review_status",
            "final_label",
            "stale",
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
        ],
        limit_page_length=1_000,
    )


def _pair_stale(row: Any) -> bool:
    current = frappe.db.get_value(
        "CCD Master", row.left_record, "modified"
    ), frappe.db.get_value("CCD Master", row.right_record, "modified")
    return (
        str(current[0] or "") != str(row.left_modified_at or "")
        or str(current[1] or "") != str(row.right_modified_at or "")
    )


@frappe.whitelist(methods=["POST"])
def finalize_splink_automatic_validation(run_name: str) -> dict[str, Any]:
    """Compute the frozen prospective gate without granting approval."""
    _require_manager()
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != "Splink Automatic Validation" or run.status not in {
        "Reviewing",
        "Awaiting Management Approval",
    }:
        frappe.throw("Only a reviewing Splink Automatic Validation can be finalized")
    current, provenance_reasons = _authorization_current(run)
    if not current:
        _mark_validation_stale(run, provenance_reasons)
        frappe.throw("Splink authorization provenance is stale: " + ", ".join(provenance_reasons))
    rows = _validation_rows(run.name)
    stale = 0
    labels: list[str] = []
    unresolved = 0
    for row in rows:
        is_stale = bool(row.stale) or _pair_stale(row)
        if is_stale:
            stale += 1
            if not row.stale:
                frappe.db.set_value(PAIR_DOCTYPE, row.name, "stale", 1, update_modified=False)
                row.stale = 1
            continue
        ordinary_reviewers = set(
            str(reviewer)
            for reviewer in frappe.get_all(
                "CCD Match Review Label",
                filters={
                    "parent": row.name,
                    "parenttype": PAIR_DOCTYPE,
                    "is_adjudication": 0,
                },
                pluck="reviewer",
                limit_page_length=10,
            )
        )
        if (
            row.review_status not in FINAL_STATUSES
            or row.final_label not in {"Same", "Different"}
            or len(ordinary_reviewers) < 2
        ):
            unresolved += 1
        else:
            labels.append(str(row.final_label))
    gate = validation_gate(labels, stale_count=stale, unresolved_count=unresolved)
    values = {
        "validation_valid_count": gate["valid"],
        "validation_stale_count": stale,
        "validation_unresolved_count": unresolved,
        "validation_same_count": gate["same"],
        "validation_different_count": gate["different"],
        "validation_precision": gate["precision"] * 100,
        "validation_wilson_lower": gate["wilson_95"][0] * 100,
        "validation_wilson_upper": gate["wilson_95"][1] * 100,
        "metrics_json": _json(
            {
                "run_purpose": "Splink Automatic Validation",
                "frozen_cutoff": float(run.frozen_automatic_cutoff),
                "accepted_global_population_risk": True,
                "subgroup_precision": _validation_subgroup_metrics(rows),
                "authorization_gate": gate,
            }
        ),
        "status": "Awaiting Management Approval" if gate["passed"] else "Completed",
        "approval_status": "Pending Management Review" if gate["passed"] else "Rejected",
        "error_summary": "" if gate["passed"] else ";".join(gate["reasons"])[:140],
    }
    frappe.db.set_value(RUN_DOCTYPE, run.name, values, update_modified=False)
    for row in rows:
        if row.review_candidate:
            frappe.db.set_value(
                CANDIDATE_DOCTYPE,
                row.review_candidate,
                "automation_status",
                "Validation Final" if row.final_label in {"Same", "Different"} and not row.stale else "Stale",
                update_modified=False,
            )
    frappe.db.commit()
    return {"run": run.name, "status": values["status"], **gate}


def _validation_subgroup_metrics(rows: list[Any]) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {}
    for row in rows:
        if row.stale or row.final_label not in {"Same", "Different"}:
            continue
        source_pair = str(
            frappe.db.get_value(PAIR_DOCTYPE, row.name, "source_pair") or "Unknown"
        )
        grouped.setdefault(source_pair, []).append(str(row.final_label))
    return {
        source: validation_gate(labels, minimum_valid=1, lower_target=0.0)
        for source, labels in sorted(grouped.items())
    }


@frappe.whitelist(methods=["POST"])
def approve_splink_automatic_validation(
    run_name: str, decision: str, reason: str, confirm_run_name: str
) -> dict[str, Any]:
    _require_manager()
    if str(confirm_run_name or "").strip() != str(run_name or "").strip():
        frappe.throw("Type the exact Validation Run ID to confirm management approval")
    if decision not in {"Approved", "Rejected"}:
        frappe.throw("Decision must be Approved or Rejected")
    if not str(reason or "").strip():
        frappe.throw("A management decision reason is required")
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Awaiting Management Approval":
        frappe.throw("The validation is not awaiting management approval")
    current, reasons = _authorization_current(run)
    if not current:
        _mark_validation_stale(run, reasons)
        frappe.throw("Splink authorization provenance is stale: " + ", ".join(reasons))
    metrics = json.loads(run.metrics_json or "{}")
    gate = metrics.get("authorization_gate") or {}
    if decision == "Approved" and not gate.get("passed"):
        frappe.throw("The frozen validation precision gate did not pass")
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {"status": "Completed", "approval_status": decision},
        update_modified=False,
    )
    event = _append_event(
        entity_doctype=RUN_DOCTYPE,
        entity_name=run.name,
        event_type="Approve" if decision == "Approved" else "Reject",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(f"{run.name}\x1f{decision}\x1f{reason}".encode()).hexdigest(),
        from_status="Awaiting Management Approval",
        to_status=decision,
        metadata={"authorization_fingerprint": run.authorization_fingerprint, "gate": gate},
    )
    frappe.db.commit()
    return {"run": run.name, "status": "Completed", "approval_status": decision, "event": event}


def _candidate_plan(candidate: Any, *, same: bool) -> dict[str, Any]:
    records = [str(candidate.left_record), str(candidate.right_record)]
    fingerprints = expected_identity_fingerprints(
        (
            (records[0], candidate.left_identity_fingerprint),
            (records[1], candidate.right_identity_fingerprint),
        )
    )
    return {
        "record_ids": records,
        "groups": [records] if same else [[record] for record in records],
        "exclusions": [] if same else [(records[0], records[1])],
        "expected_fingerprints": fingerprints,
        "expected_modified": {
            records[0]: str(candidate.left_modified_at or ""),
            records[1]: str(candidate.right_modified_at or ""),
        },
    }


def _preview_candidate(
    candidate: Any, *, origin: str, same: bool, origin_document: str | None = None
) -> dict[str, Any]:
    plan = _candidate_plan(candidate, same=same)
    run = frappe.get_doc(RUN_DOCTYPE, candidate.automation_validation_run)
    preview = preview_materialization(
        origin=origin,
        origin_doctype=PAIR_DOCTYPE if origin == "Splink Validation" else CANDIDATE_DOCTYPE,
        origin_document=str(origin_document or candidate.name),
        policy_snapshot_json=run.policy_snapshot_json,
        record_ids=plan["record_ids"],
        groups=plan["groups"],
        exclusions=plan["exclusions"],
        expected_fingerprints=plan["expected_fingerprints"],
        expected_modified=plan["expected_modified"],
    )
    return {**plan, **preview}


def _batch_fingerprint(batch_type: str, run: Any, names: Iterable[str]) -> str:
    return hashlib.sha256(
        f"{batch_type}\x1f{run.name}\x1f{run.authorization_fingerprint}\x1f"
        f"{'|'.join(sorted(str(name) for name in names))}".encode()
    ).hexdigest()


def _create_batch(
    *, batch_type: str, run: Any, planned: list[dict[str, Any]]
) -> Any:
    names = [name for item in planned for name in item["candidate_names"]]
    selection = _batch_fingerprint(batch_type, run, names)
    existing = frappe.db.get_value(BATCH_DOCTYPE, {"selection_fingerprint": selection}, "name")
    if existing:
        return frappe.get_doc(BATCH_DOCTYPE, existing)
    batch = frappe.get_doc(
        {
            "doctype": BATCH_DOCTYPE,
            "batch_type": batch_type,
            "validation_run": run.name,
            "queue_run": run.validation_queue_run,
            "matching_policy": run.matching_policy,
            "frozen_cutoff": run.frozen_automatic_cutoff,
            "authorization_fingerprint": run.authorization_fingerprint,
            "selection_fingerprint": selection,
            "idempotency_key": hashlib.sha256(f"splink-batch\x1f{selection}".encode()).hexdigest(),
            "status": "Previewed",
            "zero_write_preview": 1,
            "component_count": len(planned),
            "candidate_count": len(names),
            "eligible_count": sum(item["eligible"] for item in planned),
            "previewed_at": now_datetime(),
            "previewed_by": frappe.session.user,
            "preview_json": _json(planned),
        }
    )
    for item in planned:
        batch.append(
            "items",
            {
                "component_fingerprint": item["component_fingerprint"],
                "component_size": item["component_size"],
                "complete_clique": int(item["complete_clique"]),
                "candidate_names_json": _json(item["candidate_names"]),
                "record_ids_json": _json(item["record_ids"]),
                "status": "Planned" if item["eligible"] else "Ineligible",
                "error_code": item.get("error", "")[:140],
            },
        )
    batch.insert(ignore_permissions=True)
    for item in planned:
        for name in item["candidate_names"]:
            frappe.db.set_value(
                CANDIDATE_DOCTYPE,
                name,
                {
                    "automation_batch": batch.name,
                    "automation_status": (
                        "Rollout Planned" if item["eligible"] else "Ineligible"
                    ),
                    "automation_ineligibility_reason": (
                        None if item["eligible"] else item.get("error", "")[:140]
                    ),
                },
                update_modified=False,
            )
    return batch


@frappe.whitelist(methods=["POST"])
def preview_validation_label_activation(run_name: str) -> dict[str, Any]:
    """Create an identity-zero-write batch from final prospective labels."""
    _require_manager()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Completed" or run.approval_status != "Approved":
        frappe.throw("Validation must pass and receive management approval first")
    current, reasons = _authorization_current(run)
    if not current:
        _mark_validation_stale(run, reasons)
        frappe.throw("Splink authorization provenance is stale: " + ", ".join(reasons))
    planned = []
    for pair in _validation_rows(run.name):
        if pair.stale or pair.final_label not in {"Same", "Different"}:
            continue
        candidate = frappe.get_doc(CANDIDATE_DOCTYPE, pair.review_candidate)
        preview = _preview_candidate(
            candidate,
            origin="Splink Validation",
            same=pair.final_label == "Same",
            origin_document=pair.name,
        )
        planned.append(
            {
                "component_fingerprint": str(candidate.automation_component_fingerprint),
                "component_size": 2,
                "complete_clique": True,
                "candidate_names": [candidate.name],
                "validation_pair": pair.name,
                "final_label": pair.final_label,
                "record_ids": preview["record_ids"],
                "eligible": bool(preview["safe"]),
                "already_applied": bool(preview["already_applied"]),
                "error": ",".join(preview["conflicts"]),
            }
        )
    batch = _create_batch(batch_type="Validation Label Activation", run=run, planned=planned)
    frappe.db.commit()
    return {
        "batch": batch.name,
        "status": batch.status,
        "zero_write_identity_preview": True,
        "candidate_count": len(planned),
        "eligible_count": sum(item["eligible"] for item in planned),
        "exception_count": sum(not item["eligible"] for item in planned),
    }


def _holdout_candidates(run: Any) -> list[Any]:
    return frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={
            "automation_validation_run": run.name,
            "automation_cohort": "Blinded Rollout Holdout",
            "automation_reserved": 1,
        },
        fields=["*"],
        order_by="name",
        limit_page_length=HOLDOUT_SIZE,
    )


@frappe.whitelist(methods=["POST"])
def create_capped_splink_rollout(run_name: str) -> dict[str, Any]:
    """Preview and freeze the blinded 50-pair rollout; identity writes remain zero."""
    _require_manager()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Completed" or run.approval_status != "Approved":
        frappe.throw("Validation must pass and receive management approval first")
    current, reasons = _authorization_current(run)
    if not current:
        _mark_validation_stale(run, reasons)
        frappe.throw("Splink authorization provenance is stale: " + ", ".join(reasons))
    candidates = _holdout_candidates(run)
    if len(candidates) != HOLDOUT_SIZE:
        frappe.throw(f"The blinded holdout must contain exactly {HOLDOUT_SIZE} reserved pairs")
    planned = []
    for candidate in candidates:
        reasons = []
        if candidate.stale:
            reasons.append("candidate_stale")
        if int(candidate.automation_component_size or 0) != 2:
            reasons.append("initial_rollout_requires_isolated_pair")
        if not candidate.automation_complete_clique:
            reasons.append("component_not_complete_clique")
        if float(candidate.probabilistic_score or 0) < float(run.frozen_automatic_cutoff):
            reasons.append("below_frozen_cutoff")
        preview = _preview_candidate(candidate, origin="Splink Automated", same=True)
        reasons.extend(preview["conflicts"])
        planned.append(
            {
                "component_fingerprint": str(candidate.automation_component_fingerprint),
                "component_size": 2,
                "complete_clique": True,
                "candidate_names": [str(candidate.name)],
                "record_ids": preview["record_ids"],
                "eligible": not reasons and bool(preview["safe"]),
                "already_applied": bool(preview["already_applied"]),
                "error": ",".join(sorted(set(reasons))),
            }
        )
    eligible = sum(item["eligible"] for item in planned)
    frappe.db.set_value(RUN_DOCTYPE, run.name, "holdout_eligible_count", eligible, update_modified=False)
    if eligible < MIN_ELIGIBLE_HOLDOUT:
        frappe.throw(
            f"Only {eligible} blinded holdout pairs remain eligible; at least {MIN_ELIGIBLE_HOLDOUT} are required"
        )
    batch = _create_batch(batch_type="Capped Holdout Rollout", run=run, planned=planned)
    frappe.db.commit()
    return {
        "batch": batch.name,
        "status": batch.status,
        "zero_write_identity_preview": True,
        "holdout_count": len(candidates),
        "eligible_count": eligible,
        "minimum_eligible": MIN_ELIGIBLE_HOLDOUT,
    }


@frappe.whitelist(methods=["POST"])
def approve_and_queue_splink_batch(
    batch_name: str, reason: str, confirm_batch_name: str
) -> dict[str, Any]:
    """Approve the frozen preview and enqueue its idempotent background apply."""
    _require_manager()
    if str(confirm_batch_name or "").strip() != str(batch_name or "").strip():
        frappe.throw("Type the exact Splink Automation Batch ID to confirm")
    if not str(reason or "").strip():
        frappe.throw("A batch approval reason is required")
    _lock_rows(BATCH_DOCTYPE, (batch_name,))
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.status in {"Queued", "Applying", "Applied", "Applied with Exceptions"}:
        return {"batch": batch.name, "status": batch.status, "already_queued": True}
    if batch.status != "Previewed":
        frappe.throw("Only a fresh zero-write preview may be approved")
    run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
    current, reasons = _authorization_current(run)
    if not current or batch.authorization_fingerprint != run.authorization_fingerprint:
        _mark_validation_stale(run, reasons or ["batch_authorization_mismatch"])
        frappe.throw("The batch authorization is no longer current")
    batch.db_set(
        {
            "status": "Queued",
            "approved_at": now_datetime(),
            "approved_by": frappe.session.user,
        },
        update_modified=False,
    )
    _append_event(
        entity_doctype=BATCH_DOCTYPE,
        entity_name=batch.name,
        event_type="Approve",
        reason=str(reason).strip(),
        nonce=batch.idempotency_key,
        from_status="Previewed",
        to_status="Queued",
        metadata={"batch_type": batch.batch_type, "authorization_fingerprint": batch.authorization_fingerprint},
    )
    frappe.enqueue(
        "db_connector.api_splink_automation.apply_splink_batch",
        queue="long",
        timeout=7_200,
        enqueue_after_commit=True,
        job_id=f"ccd-splink-batch-{batch.name}",
        batch_name=batch.name,
        requested_by=frappe.session.user,
    )
    frappe.db.commit()
    return {"batch": batch.name, "status": "Queued"}


@frappe.whitelist(methods=["POST"])
def retry_splink_batch(
    batch_name: str, reason: str, confirm_batch_name: str
) -> dict[str, Any]:
    """Requeue a failed batch without changing its frozen component selection."""
    _require_manager()
    if str(confirm_batch_name or "").strip() != str(batch_name or "").strip():
        frappe.throw("Type the exact Splink Automation Batch ID to confirm retry")
    if not str(reason or "").strip():
        frappe.throw("A retry reason is required")
    _lock_rows(BATCH_DOCTYPE, (batch_name,))
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.status in {"Queued", "Applying", "Applied", "Applied with Exceptions"}:
        return {"batch": batch.name, "status": batch.status, "already_queued": True}
    if batch.status != "Failed":
        frappe.throw("Only a failed Splink batch may be retried")
    run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
    current, reasons = _authorization_current(run)
    if not current or batch.authorization_fingerprint != run.authorization_fingerprint:
        _mark_validation_stale(run, reasons or ["batch_authorization_mismatch"])
        frappe.throw("The batch authorization is no longer current")
    frappe.db.set_value(
        BATCH_DOCTYPE,
        batch.name,
        {"status": "Queued", "error_summary": None},
        update_modified=False,
    )
    _append_event(
        entity_doctype=BATCH_DOCTYPE,
        entity_name=batch.name,
        event_type="Retry",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(
            f"splink-batch-retry\x1f{batch.name}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Failed",
        to_status="Queued",
        metadata={"batch_type": batch.batch_type},
    )
    frappe.enqueue(
        "db_connector.api_splink_automation.apply_splink_batch",
        queue="long",
        timeout=7_200,
        enqueue_after_commit=True,
        job_id=f"ccd-splink-batch-{batch.name}",
        batch_name=batch.name,
        requested_by=frappe.session.user,
    )
    frappe.db.commit()
    return {"batch": batch.name, "status": "Queued"}


def _materialize_validation_item(batch: Any, item: Any, candidate: Any) -> dict[str, Any]:
    pair_name = str(
        frappe.db.get_value(
            PAIR_DOCTYPE,
            {"evaluation_run": batch.validation_run, "review_candidate": candidate.name},
            "name",
        )
        or ""
    )
    if not pair_name:
        frappe.throw("The batch item has no frozen validation pair")
    pair = frappe.get_doc(PAIR_DOCTYPE, pair_name)
    plan = _candidate_plan(candidate, same=pair.final_label == "Same")
    run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
    policy_snapshot_sha256 = str(
        frappe.db.get_value(QUEUE_DOCTYPE, run.validation_queue_run, "policy_snapshot_sha256")
        or ""
    )
    return materialize_identity(
        origin="Splink Validation",
        origin_doctype=PAIR_DOCTYPE,
        origin_document=pair.name,
        policy_snapshot_json=run.policy_snapshot_json,
        policy_snapshot_sha256=policy_snapshot_sha256,
        matching_policy=run.matching_policy,
        record_ids=plan["record_ids"],
        groups=plan["groups"],
        exclusions=plan["exclusions"],
        expected_fingerprints=plan["expected_fingerprints"],
        expected_modified=plan["expected_modified"],
        reason_codes=["prospective_validation_human_" + str(pair.final_label).casefold()],
        review_context={"validation_run": run.name, "validation_pair": pair.name},
    )


def _materialize_automated_item(batch: Any, item: Any, candidates: list[Any]) -> dict[str, Any]:
    run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
    policy_snapshot_sha256 = str(
        frappe.db.get_value(QUEUE_DOCTYPE, run.validation_queue_run, "policy_snapshot_sha256")
        or ""
    )
    records = sorted(
        {str(record) for candidate in candidates for record in (candidate.left_record, candidate.right_record)}
    )
    expected_modified: dict[str, str] = {}
    fingerprint_rows = []
    for candidate in candidates:
        for record, modified, fingerprint in (
            (candidate.left_record, candidate.left_modified_at, candidate.left_identity_fingerprint),
            (candidate.right_record, candidate.right_modified_at, candidate.right_identity_fingerprint),
        ):
            key = str(record)
            value = str(modified or "")
            if key in expected_modified and expected_modified[key] != value:
                frappe.throw("The frozen component has inconsistent modified timestamps")
            expected_modified[key] = value
            fingerprint_rows.append((key, fingerprint))
    fingerprints = expected_identity_fingerprints(fingerprint_rows)
    candidate_names = sorted(str(candidate.name) for candidate in candidates)
    return materialize_identity(
        origin="Splink Automated",
        origin_doctype=CANDIDATE_DOCTYPE,
        origin_document=candidate_names[0],
        policy_snapshot_json=run.policy_snapshot_json,
        policy_snapshot_sha256=policy_snapshot_sha256,
        matching_policy=run.matching_policy,
        record_ids=records,
        groups=[records],
        exclusions=[],
        expected_fingerprints=fingerprints,
        expected_modified=expected_modified,
        reason_codes=["validated_probabilistic_automation", "ultra_high_frozen_cutoff"],
        review_context={
            "validation_run": run.name,
            "automation_batch": batch.name,
            "component_fingerprint": item.component_fingerprint,
            "candidate_names": candidate_names,
            "frozen_cutoff": float(run.frozen_automatic_cutoff),
        },
    )


def apply_splink_batch(batch_name: str, requested_by: str) -> dict[str, Any]:
    """Apply component-atomic batch items with restart-safe identity keys."""
    if "System Manager" not in set(frappe.get_roles(requested_by)):
        raise frappe.PermissionError("System Manager role is required")
    previous_user = frappe.session.user
    frappe.set_user(requested_by)
    try:
        _lock_rows(BATCH_DOCTYPE, (batch_name,))
        batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
        if batch.status in {"Applied", "Applied with Exceptions"}:
            return {"batch": batch.name, "status": batch.status, "already_applied": True}
        if batch.status == "Applying":
            return {"batch": batch.name, "status": "Applying", "already_running": True}
        if batch.status not in {"Queued", "Failed"}:
            frappe.throw("Only an approved queued Splink batch may be applied")
        if not materialization_enabled(
            automated=batch.batch_type != "Validation Label Activation",
            channel="splink",
        ):
            frappe.throw("Live materialization or the applicable Splink control is disabled")
        run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
        current, reasons = _authorization_current(run)
        if not current or batch.authorization_fingerprint != run.authorization_fingerprint:
            _mark_validation_stale(run, reasons or ["batch_authorization_mismatch"])
            frappe.throw("The batch authorization is no longer current")
        batch.db_set("status", "Applying", update_modified=False)
        frappe.db.commit()
        applied = exceptions = stale = 0
        for item in batch.items:
            if item.status in {"Applied", "Already Applied"}:
                applied += 1
                continue
            if item.status == "Ineligible":
                exceptions += 1
                continue
            savepoint = f"splink_item_{int(item.idx)}"
            frappe.db.savepoint(savepoint)
            try:
                names = json.loads(item.candidate_names_json or "[]")
                _lock_rows(CANDIDATE_DOCTYPE, names)
                candidates = [frappe.get_doc(CANDIDATE_DOCTYPE, name) for name in names]
                if batch.batch_type == "Validation Label Activation":
                    result = _materialize_validation_item(batch, item, candidates[0])
                else:
                    result = _materialize_automated_item(batch, item, candidates)
                item_status = "Already Applied" if result["status"] == "Already Applied" else "Applied"
                frappe.db.set_value(
                    ITEM_DOCTYPE,
                    item.name,
                    {
                        "status": item_status,
                        "identity_decisions_json": _json([result.get("identity_decision")]),
                        "identity_groups_json": _json(
                            result.get("identity_groups")
                            or json.loads(candidates[0].identity_groups_json or "[]")
                        ),
                        "error_code": None,
                    },
                    update_modified=False,
                )
                for candidate in candidates:
                    values = {
                        "automation_status": "Applied",
                        "identity_decision": result.get("identity_decision"),
                        "materialization_status": "Applied",
                    }
                    if result.get("identity_groups") is not None:
                        values["identity_groups_json"] = _json(
                            result.get("identity_groups") or []
                        )
                    if batch.batch_type != "Validation Label Activation":
                        values.update(
                            {
                                "automated_applied_at": now_datetime(),
                                "automated_qc_review_status": "Unreviewed",
                            }
                        )
                    frappe.db.set_value(CANDIDATE_DOCTYPE, candidate.name, values, update_modified=False)
                applied += 1
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                code = f"{type(exc).__name__}:{str(exc)}"[:140]
                is_stale = any(
                    marker in str(exc)
                    for marker in (
                        "identity_fingerprint_changed",
                        "source_modified_after_snapshot",
                        "frozen_identity_snapshot_incomplete",
                    )
                )
                frappe.db.set_value(
                    ITEM_DOCTYPE,
                    item.name,
                    {"status": "Stale" if is_stale else "Exception", "error_code": code},
                    update_modified=False,
                )
                for name in json.loads(item.candidate_names_json or "[]"):
                    frappe.db.set_value(
                        CANDIDATE_DOCTYPE,
                        name,
                        {
                            "automation_status": "Stale" if is_stale else "Exception",
                            "automation_ineligibility_reason": code,
                        },
                        update_modified=False,
                    )
                if is_stale:
                    stale += 1
                else:
                    exceptions += 1
            frappe.db.commit()
        status = "Applied" if not exceptions and not stale else "Applied with Exceptions"
        frappe.db.set_value(
            BATCH_DOCTYPE,
            batch.name,
            {
                "status": status,
                "applied_count": applied,
                "exception_count": exceptions,
                "stale_count": stale,
                "applied_at": now_datetime(),
                "applied_by": requested_by,
            },
            update_modified=False,
        )
        if batch.batch_type != "Validation Label Activation":
            _set_settings(
                {
                    "last_splink_run_at": now_datetime(),
                    "last_splink_batch": batch.name,
                    "last_splink_status": status,
                    "last_splink_error": None,
                }
            )
        from db_connector.ccd_dashboard_snapshot import mark_dirty_after_commit

        mark_dirty_after_commit(reason="splink-automation-batch-applied")
        frappe.db.commit()
        return {"batch": batch.name, "status": status, "applied": applied, "exceptions": exceptions}
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            BATCH_DOCTYPE,
            batch_name,
            {"status": "Failed", "error_summary": f"{type(exc).__name__}:{str(exc)}"[:140]},
            update_modified=False,
        )
        _set_settings(
            {"last_splink_status": "Failed", "last_splink_error": f"{type(exc).__name__}:{str(exc)}"[:140]}
        )
        frappe.log_error(traceback.format_exc(), "Guarded Splink batch failed")
        frappe.db.commit()
        raise
    finally:
        frappe.set_user(previous_user)


def _pause_splink(reason: str, *, candidate: str = "", investigation: str = "") -> dict[str, Any]:
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.splink_automation_paused:
        return {"already_paused": True, "event": str(settings.splink_automation_authorization_event or "")}
    revision = int(settings.splink_control_revision or 0) + 1
    _set_settings(
        {
            "splink_automation_paused": 1,
            "splink_pause_reason": str(reason)[:140],
            "splink_control_revision": revision,
            "last_splink_status": "Paused",
            "last_splink_error": str(reason)[:140],
        }
    )
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Pause",
        reason=str(reason),
        nonce=hashlib.sha256(f"splink-pause\x1f{revision}\x1f{reason}".encode()).hexdigest(),
        from_status="Monitoring",
        to_status="Paused",
        metadata={"channel": "Splink", "candidate": candidate, "investigation": investigation},
    )
    return {"already_paused": False, "event": event, "revision": revision}


def _candidate_qc_state(candidate: Any) -> None:
    ordinary = [row for row in candidate.automated_qc_review_labels if not row.is_adjudication]
    adjudications = [row for row in candidate.automated_qc_review_labels if row.is_adjudication]
    if adjudications:
        candidate.automated_qc_review_status = "Adjudicated"
        candidate.automated_qc_final_label = adjudications[-1].label
        return
    labels = [str(row.label) for row in ordinary]
    candidate.automated_qc_final_label = ""
    if len(labels) < 2:
        candidate.automated_qc_review_status = "Partially Reviewed" if labels else "Unreviewed"
    elif "Unsure" in labels or len(set(labels)) > 1:
        candidate.automated_qc_review_status = "Needs Adjudication"
    else:
        candidate.automated_qc_review_status = "Agreed"
        candidate.automated_qc_final_label = labels[0]


@frappe.whitelist(methods=["POST"])
def submit_splink_qc(candidate_name: str, label: str, notes: str = "") -> dict[str, Any]:
    _require_reviewer()
    if label not in {"Same", "Different", "Unsure"}:
        frappe.throw("QC label must be Same, Different, or Unsure")
    _lock_rows(CANDIDATE_DOCTYPE, (candidate_name,))
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if not candidate.automated_qc_assigned_at or candidate.automated_qc_review_status not in {
        "Unreviewed",
        "Partially Reviewed",
    }:
        frappe.throw("This Splink QC case is not open for ordinary review")
    ordinary = [row for row in candidate.automated_qc_review_labels if not row.is_adjudication]
    if any(str(row.reviewer) == str(frappe.session.user) for row in ordinary):
        frappe.throw("Your immutable Splink QC review is already recorded")
    candidate.append(
        "automated_qc_review_labels",
        {
            "reviewer": frappe.session.user,
            "label": label,
            "notes": str(notes or "").strip(),
            "submitted_at": now_datetime(),
            "is_adjudication": 0,
        },
    )
    _candidate_qc_state(candidate)
    if candidate.automated_qc_review_status in FINAL_STATUSES:
        candidate.automated_qc_finalized_at = now_datetime()
        candidate.automation_status = "QC Complete"
    candidate.save(ignore_permissions=True)
    breaker = None
    if candidate.automated_qc_final_label == "Different":
        breaker = _apply_splink_qc_breaker(candidate)
    _refresh_holdout_qc(candidate.automation_validation_run)
    frappe.db.commit()
    return {"candidate": candidate.name, "status": candidate.automated_qc_review_status, "breaker": breaker}


@frappe.whitelist(methods=["POST"])
def adjudicate_splink_qc(candidate_name: str, label: str, notes: str) -> dict[str, Any]:
    _require_manager()
    if label not in {"Same", "Different"} or not str(notes or "").strip():
        frappe.throw("QC adjudication requires Same or Different and notes")
    _lock_rows(CANDIDATE_DOCTYPE, (candidate_name,))
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if candidate.automated_qc_review_status != "Needs Adjudication":
        frappe.throw("Only Splink QC cases awaiting adjudication may be adjudicated")
    if len(
        {
            str(row.reviewer)
            for row in candidate.automated_qc_review_labels
            if not row.is_adjudication
        }
    ) < 2:
        frappe.throw("Splink QC requires two independent masked reviews before adjudication")
    candidate.append(
        "automated_qc_review_labels",
        {
            "reviewer": frappe.session.user,
            "label": label,
            "notes": str(notes).strip(),
            "submitted_at": now_datetime(),
            "is_adjudication": 1,
        },
    )
    _candidate_qc_state(candidate)
    candidate.automated_qc_finalized_at = now_datetime()
    candidate.automation_status = "QC Complete"
    candidate.save(ignore_permissions=True)
    breaker = _apply_splink_qc_breaker(candidate) if label == "Different" else None
    _refresh_holdout_qc(candidate.automation_validation_run)
    frappe.db.commit()
    return {"candidate": candidate.name, "status": candidate.automated_qc_review_status, "breaker": breaker}


def _apply_splink_qc_breaker(candidate: Any) -> dict[str, Any]:
    if candidate.automated_qc_failure_action:
        return {"status": "Already Applied", "action": candidate.automated_qc_failure_action}
    memberships = _current_memberships((str(candidate.left_record), str(candidate.right_record)))
    shared = current_shared_group(
        (dict(row) for row in memberships), str(candidate.left_record), str(candidate.right_record)
    )
    key = hashlib.sha256(f"splink-qc\x1f{candidate.name}".encode()).hexdigest()
    investigation = frappe.db.get_value(INVESTIGATION_DOCTYPE, {"investigation_key": key}, "name")
    if not investigation:
        investigation = frappe.get_doc(
            {
                "doctype": INVESTIGATION_DOCTYPE,
                "investigation_key": key,
                "automation_channel": "Splink",
                "splink_candidate": candidate.name,
                "splink_validation_run": candidate.automation_validation_run,
                "identity_decision": candidate.identity_decision or None,
                "identity_group": shared or None,
                "pause_scope": f"splink:{candidate.source_pair}",
                "status": "Open",
                "reason": f"confirmed_splink_qc_different:{candidate.name}",
                "opened_at": now_datetime(),
                "opened_by": frappe.session.user or "Administrator",
            }
        ).insert(ignore_permissions=True).name
    pause = _pause_splink(
        f"confirmed_splink_qc_different:{candidate.name}",
        candidate=candidate.name,
        investigation=str(investigation),
    )
    suspended = 0
    if shared:
        from db_connector.api_identity_qc import _suspend_group_for_qc

        suspended = _suspend_group_for_qc(str(shared), str(investigation))
    action = (
        f"splink_paused;investigation={investigation};shared_group={shared or 'none'};"
        f"memberships_needing_revalidation={suspended};pause_event={pause.get('event', '')}"
    )
    candidate.db_set("automated_qc_failure_action", action, update_modified=False)
    return {"status": "Paused", "investigation": investigation, "identity_group": shared or "", "suspended": suspended}


def _refresh_holdout_qc(run_name: str) -> dict[str, Any]:
    if not run_name:
        return {}
    rows = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={"automation_validation_run": run_name, "automation_cohort": "Blinded Rollout Holdout"},
        fields=["automated_qc_review_status", "automated_qc_final_label"],
        limit_page_length=HOLDOUT_SIZE,
    )
    complete = [row for row in rows if row.automated_qc_review_status in FINAL_STATUSES]
    same = sum(row.automated_qc_final_label == "Same" for row in complete)
    different = sum(row.automated_qc_final_label == "Different" for row in complete)
    frappe.db.set_value(
        RUN_DOCTYPE,
        run_name,
        {
            "holdout_qc_complete_count": len(complete),
            "holdout_qc_same_count": same,
            "holdout_qc_different_count": different,
        },
        update_modified=False,
    )
    return {"complete": len(complete), "same": same, "different": different}


def _week_start() -> Any:
    today = frappe.utils.getdate()
    return add_days(today, -today.weekday())


def _weekly_assigned_count() -> int:
    start = _week_start()
    recommendation = frappe.db.count(RECOMMENDATION_DOCTYPE, {"qc_assigned_at": [">=", start]})
    splink = frappe.db.count(CANDIDATE_DOCTYPE, {"automated_qc_assigned_at": [">=", start]})
    optional = frappe.db.count(
        CANDIDATE_DOCTYPE,
        {"assigned_at": [">=", start], "automation_reserved": 0},
    )
    return int(recommendation) + int(splink) + int(optional)


def _mandatory_unassigned() -> tuple[list[Any], list[Any]]:
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.automatic_tiered_canary:
        from db_connector.api_identity_qc import _replenish_qc_pool

        currently_available = frappe.db.count(
            RECOMMENDATION_DOCTYPE,
            {
                "canary_run": settings.automatic_tiered_canary,
                "qc_selected": 1,
                "qc_stale": 0,
                "qc_assigned_at": ["is", "not set"],
            },
        )
        _replenish_qc_pool(
            str(settings.automatic_tiered_canary),
            max(WEEKLY_PAIR_CAPACITY - int(currently_available), 0),
        )
    recommendations = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={
            "qc_selected": 1,
            "qc_stale": 0,
            "qc_assigned_at": ["is", "not set"],
            "qc_review_status": ["in", ["Unreviewed", "Partially Reviewed", "Needs Adjudication", "Positive Confirmation Required"]],
        },
        fields=["name", "recommendation_key", "canary_run"],
        limit_page_length=100_000,
    )
    splink = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={
            "automation_status": "Applied",
            "automated_qc_review_status": "Unreviewed",
            "automated_qc_assigned_at": ["is", "not set"],
        },
        fields=["name", "pair_key", "automation_validation_run"],
        limit_page_length=100_000,
    )
    return recommendations, splink


def _deterministic_names(channel: str, rows: list[Any], limit: int) -> list[str]:
    week = str(_week_start())
    return [
        str(row.name)
        for row in sorted(
            rows,
            key=lambda row: (
                hashlib.sha256(
                    f"{week}\x1f{channel}\x1f{getattr(row, 'recommendation_key', '') or getattr(row, 'pair_key', '') or row.name}".encode()
                ).hexdigest(),
                str(row.name),
            ),
        )[:limit]
    ]


def run_shared_review_capacity() -> dict[str, Any]:
    """Assign mandatory QC first within one hard 20-pair weekly capacity."""
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if not settings.automatic_qc_assignment_enabled:
        return {"status": "Disabled", "assigned": 0}
    used = _weekly_assigned_count()
    available = max(WEEKLY_PAIR_CAPACITY - used, 0)
    recommendations, splink = _mandatory_unassigned()
    week = _week_start()
    recent_volume = {
        "Recommendation": int(
            frappe.db.count(
                RECOMMENDATION_DOCTYPE,
                {"rollout_state": "Applied", "activated_at": [">=", week]},
            )
        ),
        "Splink": int(
            frappe.db.count(
                CANDIDATE_DOCTYPE, {"automated_applied_at": [">=", week]}
            )
        ),
    }
    allocation = allocate_shared_capacity(
        {"Recommendation": len(recommendations), "Splink": len(splink)},
        capacity=available,
        recent_unattended_volume=recent_volume,
    )
    now = now_datetime()
    due = add_days(now, int(settings.qc_sla_days or 14))
    rec_names = _deterministic_names("Recommendation", recommendations, allocation["Recommendation"])
    splink_names = _deterministic_names("Splink", splink, allocation["Splink"])
    for name in rec_names:
        frappe.db.set_value(
            RECOMMENDATION_DOCTYPE, name, {"qc_assigned_at": now, "qc_due_at": due}, update_modified=False
        )
    for name in splink_names:
        frappe.db.set_value(
            CANDIDATE_DOCTYPE,
            name,
            {"automated_qc_assigned_at": now, "automated_qc_due_at": due, "automation_status": "QC Assigned"},
            update_modified=False,
        )
    mandatory_assigned = len(rec_names) + len(splink_names)
    optional_limit = max(available - mandatory_assigned, 0)
    optional_rows = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={
            "stale": 0,
            "automation_reserved": 0,
            "assigned_review_batch": ["is", "not set"],
            "assigned_at": ["is", "not set"],
            "review_status": [
                "in",
                [
                    "Unreviewed",
                    "Partially Reviewed",
                    "Positive Confirmation Required",
                    "Needs Adjudication",
                ],
            ],
        },
        fields=["name"],
        order_by="priority_rank asc, name asc",
        limit=optional_limit,
    )
    optional_names = [str(row.name) for row in optional_rows]
    for name in optional_names:
        frappe.db.set_value(
            CANDIDATE_DOCTYPE,
            name,
            {"assigned_at": now, "due_at": due},
            update_modified=False,
        )
    assigned = mandatory_assigned + len(optional_names)
    frappe.db.commit()
    return {
        "status": "Assigned" if assigned else "No Work",
        "weekly_capacity": WEEKLY_PAIR_CAPACITY,
        "already_used": used,
        "recent_unattended_volume": recent_volume,
        "assigned": assigned,
        "recommendation_qc": rec_names,
        "splink_qc": splink_names,
        "optional_splink": optional_names,
        "remaining_capacity": available - assigned,
    }


def _holdout_acceptance_blockers(run: Any) -> list[str]:
    blockers = []
    candidates = _holdout_candidates(run)
    applied = [candidate for candidate in candidates if candidate.automated_applied_at]
    if len(applied) < MIN_ELIGIBLE_HOLDOUT:
        blockers.append(f"insufficient_applied_holdout:{len(applied)}:{MIN_ELIGIBLE_HOLDOUT}")
    unresolved = [candidate for candidate in applied if candidate.automated_qc_review_status not in FINAL_STATUSES]
    if unresolved:
        blockers.append(f"unresolved_holdout_qc:{len(unresolved)}")
    open_investigations = frappe.db.count(
        INVESTIGATION_DOCTYPE,
        {"automation_channel": "Splink", "splink_validation_run": run.name, "status": "Open"},
    )
    if open_investigations:
        blockers.append(f"open_splink_investigations:{open_investigations}")
    current_validation_labels = [
        str(row.final_label)
        for row in _validation_rows(run.name)
        if not row.stale
        and not _pair_stale(row)
        and row.review_status in FINAL_STATUSES
        and row.final_label in {"Same", "Different"}
    ]
    current_validation_gate = validation_gate(current_validation_labels)
    if not current_validation_gate["passed"]:
        blockers.extend(
            "validation_" + reason for reason in current_validation_gate["reasons"]
        )
    combined = list(current_validation_labels)
    excluded = set(
        str(name)
        for name in frappe.get_all(
            INVESTIGATION_DOCTYPE,
            filters={
                "automation_channel": "Splink",
                "splink_validation_run": run.name,
                "status": "Resolved",
                "resolution_action": "QC Review Error",
            },
            pluck="splink_candidate",
            limit_page_length=10_000,
        )
        if name
    )
    combined.extend(
        str(candidate.automated_qc_final_label)
        for candidate in applied
        if candidate.automated_qc_final_label and str(candidate.name) not in excluded
    )
    gate = validation_gate(combined)
    if not gate["passed"]:
        blockers.extend("combined_" + reason for reason in gate["reasons"])
    return blockers


@frappe.whitelist(methods=["POST"])
def set_scheduled_splink_approval(
    run_name: str, decision: str, reason: str, confirm_run_name: str
) -> dict[str, Any]:
    _require_manager()
    if str(confirm_run_name or "").strip() != str(run_name or "").strip():
        frappe.throw("Type the exact Validation Run ID to confirm")
    if decision not in {"Approved", "Rejected"} or not str(reason or "").strip():
        frappe.throw("A valid decision and management reason are required")
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    blockers = _holdout_acceptance_blockers(run)
    if decision == "Approved" and blockers:
        frappe.throw("Scheduled Splink approval is blocked: " + ", ".join(blockers))
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            "second_management_approval_status": decision,
            "second_management_approved_at": now_datetime(),
            "second_management_approved_by": frappe.session.user,
        },
        update_modified=False,
    )
    event = _append_event(
        entity_doctype=RUN_DOCTYPE,
        entity_name=run.name,
        event_type="Approve" if decision == "Approved" else "Reject",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(f"scheduled-splink\x1f{run.name}\x1f{decision}\x1f{reason}".encode()).hexdigest(),
        from_status="Holdout QC",
        to_status=decision,
        metadata={"channel": "Splink", "blockers": blockers},
    )
    if decision == "Approved":
        _lock_settings()
        _set_settings(
            {
                "automatic_splink_enabled": 0,
                "authorized_splink_validation_run": run.name,
                "authorized_splink_queue": run.validation_queue_run,
                "authorized_splink_policy": run.matching_policy,
                "authorized_splink_cutoff": run.frozen_automatic_cutoff,
                "last_splink_status": "Authorized but Disabled",
            }
        )
    frappe.db.commit()
    return {"run": run.name, "decision": decision, "event": event, "blockers": blockers}


@frappe.whitelist(methods=["POST"])
def set_automatic_splink(
    enabled: int | str, reason: str, confirm_phrase: str
) -> dict[str, Any]:
    _require_manager()
    desired = str(enabled or "0").strip().casefold() in {"1", "true", "yes", "on"}
    exact = "ENABLE AUTOMATIC SPLINK" if desired else "DISABLE AUTOMATIC SPLINK"
    if str(confirm_phrase or "").strip() != exact:
        frappe.throw(f"Type {exact} exactly to confirm")
    if not str(reason or "").strip():
        frappe.throw("A control-change reason is required")
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    run_name = str(settings.authorized_splink_validation_run or "")
    if desired:
        if not settings.materialization_enabled:
            frappe.throw("Live identity materialization must be enabled")
        if not settings.automatic_qc_assignment_enabled:
            frappe.throw("Automatic QC assignment must be enabled")
        if not run_name:
            frappe.throw("Select an authorized Splink validation run")
        run = frappe.get_doc(RUN_DOCTYPE, run_name)
        blockers = _holdout_acceptance_blockers(run)
        if run.second_management_approval_status != "Approved":
            blockers.append("second_management_approval_missing")
        current, reasons = _authorization_current(run)
        if not current:
            blockers.extend(reasons)
        if blockers:
            frappe.throw("Automatic Splink cannot be enabled: " + ", ".join(blockers))
    revision = int(settings.splink_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Enable" if desired else "Disable",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(f"splink-control\x1f{revision}\x1f{desired}\x1f{reason}".encode()).hexdigest(),
        from_status="Enabled" if settings.automatic_splink_enabled else "Disabled",
        to_status="Enabled" if desired else "Disabled",
        metadata={"channel": "Splink", "revision": revision, "validation_run": run_name},
    )
    _set_settings(
        {
            "automatic_splink_enabled": int(desired),
            "splink_control_revision": revision,
            "splink_automation_authorization_event": event if desired else None,
            "last_splink_status": "Enabled" if desired else "Disabled",
        }
    )
    frappe.db.commit()
    return {"status": "Enabled" if desired else "Disabled", "event": event, "revision": revision}


@frappe.whitelist(methods=["POST"])
def pause_splink_automation(reason: str, confirm_phrase: str) -> dict[str, Any]:
    _require_manager()
    if str(confirm_phrase or "").strip() != "PAUSE SPLINK AUTOMATION":
        frappe.throw("Type PAUSE SPLINK AUTOMATION exactly to confirm")
    if not str(reason or "").strip():
        frappe.throw("A Splink pause reason is required")
    result = _pause_splink("manager_pause:" + str(reason).strip())
    frappe.db.commit()
    return {"status": "Paused", **result}


@frappe.whitelist(methods=["POST"])
def resume_splink_automation(reason: str, confirm_phrase: str) -> dict[str, Any]:
    _require_manager()
    if str(confirm_phrase or "").strip() != "RESUME SPLINK AUTOMATION":
        frappe.throw("Type RESUME SPLINK AUTOMATION exactly to confirm")
    if not str(reason or "").strip():
        frappe.throw("A Splink resume reason is required")
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.automatic_splink_enabled:
        frappe.throw("Disable Automatic Splink before clearing its circuit breaker")
    blockers = []
    if frappe.db.count(INVESTIGATION_DOCTYPE, {"automation_channel": "Splink", "status": "Open"}):
        blockers.append("open_splink_investigations")
    if frappe.db.count(
        CANDIDATE_DOCTYPE,
        {
            "automated_qc_due_at": ["<", now_datetime()],
            "automated_qc_stale": 0,
            "automated_qc_review_status": ["not in", ["Agreed", "Adjudicated", "Stale"]],
        },
    ):
        blockers.append("overdue_splink_qc")
    if blockers:
        frappe.throw("Splink automation cannot resume: " + ", ".join(blockers))
    revision = int(settings.splink_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Resume",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(f"splink-resume\x1f{revision}\x1f{reason}".encode()).hexdigest(),
        from_status="Paused",
        to_status="Monitoring",
        metadata={"channel": "Splink", "revision": revision},
    )
    _set_settings(
        {
            "splink_automation_paused": 0,
            "splink_pause_reason": None,
            "splink_control_revision": revision,
            "last_splink_status": "Monitoring",
            "last_splink_error": None,
        }
    )
    frappe.db.commit()
    return {"status": "Monitoring", "event": event, "revision": revision}


def _scheduled_candidates(run: Any, maximum_size: int) -> list[dict[str, Any]]:
    rows = _queue_rows(run.validation_queue_run, minimum_score=float(run.frozen_automatic_cutoff))
    return [
        row
        for row in rows
        if not row.get("stale")
        and not row.get("automation_reserved")
        and not row.get("automation_batch")
        and row.get("automation_status") in {None, "", "Optional Review"}
    ]


def run_automatic_splink_cycle() -> dict[str, Any]:
    """Run after the deterministic Recommendation cycle against refreshed identity state."""
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if not settings.automatic_splink_enabled:
        return {"status": "Disabled", "selected_components": 0}
    if settings.splink_automation_paused:
        return {"status": "Paused", "selected_components": 0, "reason": settings.splink_pause_reason}
    run = frappe.get_doc(RUN_DOCTYPE, settings.authorized_splink_validation_run)
    blockers = _holdout_acceptance_blockers(run)
    current, reasons = _authorization_current(run)
    if not current:
        blockers.extend(reasons)
    if blockers:
        pause = _pause_splink("scheduled_preflight:" + ",".join(blockers))
        frappe.db.commit()
        return {"status": "Paused", "selected_components": 0, "blockers": blockers, **pause}
    maximum_size = max(2, int(settings.maximum_splink_component_size or 2))
    component_limit = min(max(int(settings.automatic_splink_components_per_run or 10), 1), 100)
    rows = _scheduled_candidates(run, maximum_size)
    components = automatic_components(rows, maximum_size=maximum_size)[:component_limit]
    if not components:
        return {"status": "No Eligible Work", "selected_components": 0}
    by_name = {str(row["name"]): row for row in rows}
    planned = []
    for component in components:
        candidates = [by_name[name] for name in component.candidate_names]
        # Component-level safety is repeated by the locked apply worker.
        planned.append(
            {
                "component_fingerprint": component.fingerprint,
                "component_size": len(component.records),
                "complete_clique": component.complete_clique,
                "candidate_names": list(component.candidate_names),
                "record_ids": list(component.records),
                "eligible": True,
                "already_applied": False,
                "error": "",
            }
        )
    batch = _create_batch(batch_type="Scheduled Splink", run=run, planned=planned)
    batch.db_set(
        {"status": "Queued", "approved_at": now_datetime(), "approved_by": "Administrator"},
        update_modified=False,
    )
    frappe.enqueue(
        "db_connector.api_splink_automation.apply_splink_batch",
        queue="long",
        timeout=7_200,
        enqueue_after_commit=True,
        job_id=f"ccd-splink-batch-{batch.name}",
        batch_name=batch.name,
        requested_by="Administrator",
    )
    frappe.db.commit()
    return {"status": "Queued", "batch": batch.name, "selected_components": len(components)}


def monitor_splink_and_run_automatic() -> dict[str, Any]:
    """Daily Splink monitor; deterministic Recommendation automation has run first."""
    result: dict[str, Any] = {"status": "Monitoring", "shared_capacity": {}, "automatic_splink": {}}
    result["shared_capacity"] = run_shared_review_capacity()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    now = now_datetime()
    overdue = frappe.db.count(
        CANDIDATE_DOCTYPE,
        {
            "automated_qc_due_at": ["<", now],
            "automated_qc_stale": 0,
            "automated_qc_review_status": ["not in", ["Agreed", "Adjudicated", "Stale"]],
        },
    )
    if overdue:
        result["pause"] = _pause_splink(f"overdue_splink_qc:{overdue}")
    finalized = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={"automated_qc_review_status": ["in", ["Agreed", "Adjudicated"]]},
        fields=["name", "automated_qc_final_label"],
        order_by="automated_qc_finalized_at asc, name asc",
        limit_page_length=10_000,
    )
    excluded = set(
        str(name)
        for name in frappe.get_all(
            INVESTIGATION_DOCTYPE,
            filters={
                "automation_channel": "Splink",
                "status": "Resolved",
                "resolution_action": "QC Review Error",
            },
            pluck="splink_candidate",
            limit_page_length=10_000,
        )
        if name
    )
    rolling = rolling_qc_summary(
        [
            str(row.automated_qc_final_label)
            for row in finalized
            if str(row.name) not in excluded
        ],
        max(int(settings.rolling_qc_window or 100), 1),
    )
    result["rolling_qc"] = rolling
    if rolling["window_complete"] and rolling["wilson_95"][0] < 0.95:
        result["pause"] = _pause_splink(
            f"splink_rolling_precision_below_0.95:{rolling['wilson_95'][0]:.6f}"
        )
    frappe.db.commit()
    result["automatic_splink"] = run_automatic_splink_cycle()
    result["status"] = "Completed"
    return result
