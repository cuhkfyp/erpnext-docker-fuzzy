"""Persistent Development-only prospective validation for lower-score hybrid rules.

The proposal preview and cohort freeze are background jobs.  This module never
creates identity Decisions, Groups, Memberships, or exclusions.
"""

from __future__ import annotations

import hashlib
import json
import re
import traceback
from collections import Counter, defaultdict
from typing import Any, Iterable

import frappe

from db_connector.api_hybrid_exploratory_review import (
    BATCH_DOCTYPE as EXPLORATORY_BATCH_DOCTYPE,
    hybrid_exploratory_candidate_names,
)
from db_connector.api_hybrid_research import (
    _active_exclusion_pairs,
    _classify_pair,
    _current_membership_records,
    _eligible_manifest_digest,
    _load_records,
    _untouched_reasons,
)
from db_connector.fuzzy_matching.hybrid_research import (
    HYBRID_RESEARCH_RULE_VERSION,
    lower_score_band,
)
from db_connector.fuzzy_matching.hybrid_validation import (
    HYBRID_HOLDOUT_SIZE,
    HYBRID_MINIMUM_ELIGIBLE_HOLDOUT,
    HYBRID_MINIMUM_SCORE,
    HYBRID_PROPOSAL_ROUTES,
    HYBRID_VALIDATION_SIZE,
    dynamic_source_allocations,
    hybrid_holdout_gate,
    select_dynamic_hybrid_cohorts,
)
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.splink_automation import (
    canonical_source_pair,
    component_metadata,
    validation_gate,
)


SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
QUEUE_DOCTYPE = "CCD Match Review Queue Run"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
RUN_DOCTYPE = "CCD Match Evaluation Run"
PAIR_DOCTYPE = "CCD Match Evaluation Pair"
LABEL_DOCTYPE = "CCD Match Review Label"
RUN_PURPOSE = "Hybrid Prospective Validation"
DOUBLE_REVIEW_REASON = "hybrid_prospective_validation"
HOLDOUT_DOUBLE_REVIEW_REASON = "hybrid_blinded_holdout_qc"
FINAL_REVIEW_STATUSES = {"Agreed", "Adjudicated"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_reviewer() -> None:
    if not {
        "System Manager",
        "CCD Match Reviewer",
        "CCD Match Sensitive Reviewer",
    }.intersection(frappe.get_roles()):
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


def _enqueue(method: str, run_name: str) -> None:
    frappe.enqueue(
        method,
        queue="long",
        timeout=3_600,
        enqueue_after_commit=True,
        run_name=run_name,
    )


def _request_context(exploratory_batch: str) -> tuple[Any, Any, Any, float, float]:
    batch = frappe.get_doc(
        EXPLORATORY_BATCH_DOCTYPE, str(exploratory_batch or "").strip()
    )
    if batch.status != "Completed":
        frappe.throw("A completed, non-stale Hybrid Exploratory Batch is required")
    if (
        int(batch.sample_count or 0) != 100
        or int(batch.reviewed_count or 0) != 100
        or int(batch.remaining_count or 0)
        or int(batch.stale_count or 0)
    ):
        frappe.throw("The exploratory batch is not a complete 100-pair research sample")
    queue = frappe.get_doc(QUEUE_DOCTYPE, batch.queue_run)
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if queue.status != "Ready":
        frappe.throw("Hybrid prospective validation requires a Ready Review Queue")
    if str(settings.authorized_splink_queue or "") != str(queue.name):
        frappe.throw("The exploratory queue is no longer the authorized Splink queue")
    floor = float(batch.research_minimum_score or 0)
    cutoff = float(batch.frozen_ultra_high_cutoff or 0)
    if abs(floor - HYBRID_MINIMUM_SCORE) > 1e-12:
        frappe.throw(
            f"The Development proposal requires the frozen {HYBRID_MINIMUM_SCORE:.12f} research floor"
        )
    if abs(float(settings.authorized_splink_cutoff or 0) - cutoff) > 1e-12:
        frappe.throw("The authorized ultra-high cutoff changed after exploration")
    policy = MatchingPolicy.from_dict(json.loads(queue.policy_snapshot_json or "{}"))
    return batch, queue, settings, floor, cutoff


def _queue_model_versions(queue: Any) -> str:
    evaluation = frappe.get_doc(RUN_DOCTYPE, queue.threshold_evaluation_run)
    return str(evaluation.model_versions_json or "{}")


def _queue_runtime_versions(queue: Any) -> str:
    from db_connector.api_splink_automation import _runtime_versions

    return _runtime_versions(queue)


def _request_fingerprint(batch: Any, queue: Any, floor: float, cutoff: float) -> str:
    payload = {
        "purpose": RUN_PURPOSE,
        "development_only": True,
        "exploratory_batch": str(batch.name),
        "exploratory_manifest": str(batch.eligible_manifest_digest or ""),
        "queue_run": str(queue.name),
        "policy_snapshot_sha256": str(queue.policy_snapshot_sha256 or ""),
        "hybrid_rule_version": HYBRID_RESEARCH_RULE_VERSION,
        "minimum_score": f"{floor:.12f}",
        "ultra_high_cutoff": f"{cutoff:.12f}",
        "routes": list(HYBRID_PROPOSAL_ROUTES),
        "validation_size": HYBRID_VALIDATION_SIZE,
        "holdout_size": HYBRID_HOLDOUT_SIZE,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _evaluation_exposed_candidate_names(queue_run: str) -> set[str]:
    return {
        str(name)
        for name in frappe.get_all(
            PAIR_DOCTYPE,
            filters={
                "review_candidate": ["is", "set"],
                "evaluation_run": ["is", "set"],
            },
            pluck="review_candidate",
            limit_page_length=100_000,
        )
        if name
    }


def _candidate_rows(queue: Any, floor: float, cutoff: float) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in frappe.get_all(
            CANDIDATE_DOCTYPE,
            filters=[
                ["queue_run", "=", queue.name],
                ["probabilistic_score", ">=", floor],
                ["probabilistic_score", "<", cutoff],
            ],
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
                "review_status",
                "final_label",
                "stale",
                "assigned_review_batch",
                "automation_reserved",
                "automation_status",
                "automation_batch",
                "materialization_status",
            ],
            order_by="probabilistic_score desc, name",
            limit_page_length=100_000,
        )
    ]


def _hybrid_inventory(batch: Any, queue: Any, floor: float, cutoff: float) -> dict[str, Any]:
    rows = _candidate_rows(queue, floor, cutoff)
    policy = MatchingPolicy.from_dict(json.loads(queue.policy_snapshot_json or "{}"))
    record_ids = {
        str(record)
        for row in rows
        for record in (row["left_record"], row["right_record"])
    }
    records = _load_records(record_ids, policy)
    exploratory_exposed = hybrid_exploratory_candidate_names(queue.name)
    evaluation_exposed = _evaluation_exposed_candidate_names(queue.name)
    allowed_routes = set(HYBRID_PROPOSAL_ROUTES)
    exclusion_counts: Counter[str] = Counter()
    graph_rows: list[dict[str, Any]] = []
    proposal_rows: list[dict[str, Any]] = []
    candidate_eligibility: dict[str, tuple[bool, list[str]]] = {}

    for row in rows:
        left_id = str(row["left_record"])
        right_id = str(row["right_record"])
        left = records.get(left_id)
        right = records.get(right_id)
        unavailable: list[str] = []
        if not left or not right:
            exclusion_counts["record_missing"] += 1
            continue
        if str(left.get("source") or "") == str(right.get("source") or ""):
            unavailable.append("same_source")
        if (
            str(left.get("modified") or "") != str(row.get("left_modified_at") or "")
            or str(right.get("modified") or "")
            != str(row.get("right_modified_at") or "")
        ):
            unavailable.append("source_modified_after_queue")
        if (
            identity_fingerprint(left, policy)
            != str(row.get("left_identity_fingerprint") or "")
            or identity_fingerprint(right, policy)
            != str(row.get("right_identity_fingerprint") or "")
        ):
            unavailable.append("identity_fingerprint_changed")
        result = _classify_pair(left, right, policy)
        if result["hard_conflicts"]:
            unavailable.extend(str(value) for value in result["hard_conflicts"])
        if unavailable:
            exclusion_counts.update(set(unavailable))
            continue

        row.update(result)
        # Component safety is evaluated across every current corroborated edge,
        # not just the narrower proposal routes.  An out-of-scope edge may still
        # make an otherwise eligible proposal edge part of a larger component.
        if result["selected_for_research"]:
            graph_rows.append(row)
        routes = tuple(sorted(allowed_routes.intersection(result["routes"])))
        if not routes:
            exclusion_counts["outside_frozen_route_scope"] += 1
            continue
        row["routes"] = routes
        row["primary_route"] = (
            "exact_name+unverified_identifier_exact"
            if "exact_name+unverified_identifier_exact" in routes
            else "name_support+unverified_identifier+secondary_exact"
        )
        row["score_band"] = lower_score_band(
            float(row["probabilistic_score"]), cutoff
        )
        row["source_pair"] = canonical_source_pair(row.get("source_pair"))
        proposal_rows.append(row)
        lifecycle_reasons = _untouched_reasons(row)
        if str(row["name"]) in exploratory_exposed:
            lifecycle_reasons.append("exploratory_sample_exposed")
        if str(row["name"]) in evaluation_exposed:
            lifecycle_reasons.append("prior_evaluation_exposed")
        candidate_eligibility[str(row["name"])] = (
            not lifecycle_reasons,
            sorted(set(lifecycle_reasons)),
        )

    graph = component_metadata(graph_rows)
    eligible_isolated: list[dict[str, Any]] = []
    for row in proposal_rows:
        candidate_name = str(row["name"])
        eligible, reasons = candidate_eligibility[candidate_name]
        if not graph[candidate_name]["isolated_pair"]:
            reasons.append("incomplete_or_multi_record_component")
            eligible = False
        if not eligible:
            exclusion_counts.update(set(reasons))
            continue
        eligible_isolated.append(row)

    eligible_record_ids = {
        str(record)
        for row in eligible_isolated
        for record in (row["left_record"], row["right_record"])
    }
    memberships = _current_membership_records(eligible_record_ids)
    exclusions = _active_exclusion_pairs(eligible_record_ids)
    operational: list[dict[str, Any]] = []
    for row in eligible_isolated:
        pair = tuple(sorted((str(row["left_record"]), str(row["right_record"]))))
        if pair[0] in memberships or pair[1] in memberships:
            exclusion_counts["existing_group_overlap"] += 1
            continue
        if pair in exclusions:
            exclusion_counts["active_human_exclusion"] += 1
            continue
        row["component_metadata"] = graph[str(row["name"])]
        operational.append(row)

    by_source = Counter(str(row["source_pair"]) for row in operational)
    by_route = Counter(str(row["primary_route"]) for row in operational)
    by_score = Counter(str(row["score_band"]) for row in operational)
    by_birthday = Counter(str(row["birthday_status"]) for row in operational)
    manifest_digest = _eligible_manifest_digest(operational)
    allocations = dynamic_source_allocations(by_source)
    return {
        "rows": operational,
        "scanned_count": len(rows),
        "rule_matching_edge_count": len(graph_rows),
        "eligible_count": len(operational),
        "eligible_manifest_digest": manifest_digest,
        "eligible_by_source": dict(sorted(by_source.items())),
        "eligible_by_route": dict(sorted(by_route.items())),
        "eligible_by_score_band": dict(sorted(by_score.items())),
        "eligible_by_birthday": dict(sorted(by_birthday.items())),
        "source_allocations": allocations,
        "exclusion_counts": dict(sorted(exclusion_counts.items())),
        "exploratory_exposed_count": len(
            {str(row["name"]) for row in proposal_rows}.intersection(exploratory_exposed)
        ),
        "prior_evaluation_exposed_count": len(
            {str(row["name"]) for row in proposal_rows}.intersection(evaluation_exposed)
        ),
    }


def _proposal_profile(
    batch: Any, queue: Any, floor: float, cutoff: float, inventory: dict[str, Any]
) -> dict[str, Any]:
    return {
        "development_only": True,
        "identity_writes": 0,
        "exploratory_batch": batch.name,
        "queue_run": queue.name,
        "hybrid_rule_version": HYBRID_RESEARCH_RULE_VERSION,
        "score_range": {
            "minimum_inclusive": floor,
            "ultra_high_exclusive": cutoff,
        },
        "frozen_routes": list(HYBRID_PROPOSAL_ROUTES),
        "validation_target": HYBRID_VALIDATION_SIZE,
        "holdout_target": HYBRID_HOLDOUT_SIZE,
        "scanned_count": inventory["scanned_count"],
        "rule_matching_edge_count": inventory["rule_matching_edge_count"],
        "eligible_count": inventory["eligible_count"],
        "eligible_manifest_digest": inventory["eligible_manifest_digest"],
        "eligible_by_source": inventory["eligible_by_source"],
        "eligible_by_route": inventory["eligible_by_route"],
        "eligible_by_score_band": inventory["eligible_by_score_band"],
        "eligible_by_birthday": inventory["eligible_by_birthday"],
        "source_allocations": inventory["source_allocations"],
        "exclusion_counts": inventory["exclusion_counts"],
        "exploratory_exposed_count": inventory["exploratory_exposed_count"],
        "prior_evaluation_exposed_count": inventory[
            "prior_evaluation_exposed_count"
        ],
        "warnings": [
            "The proposal and every resulting decision are Development-only.",
            "Exploratory and previously evaluated pairs are excluded from selection.",
            "Accepting this run cannot enable materialization or authorize UAT/Production.",
        ],
    }


@frappe.whitelist(methods=["POST"])
def start_hybrid_validation_proposal(exploratory_batch: str) -> dict[str, Any]:
    """Create/reopen a persistent proposal and build its inventory off-request."""
    _require_manager()
    batch, queue, _settings, floor, cutoff = _request_context(exploratory_batch)
    _lock_rows(EXPLORATORY_BATCH_DOCTYPE, (batch.name,))
    existing = frappe.db.get_value(
        RUN_DOCTYPE,
        {
            "run_purpose": RUN_PURPOSE,
            "hybrid_exploratory_batch": batch.name,
        },
        ["name", "status"],
        as_dict=True,
    )
    if existing:
        return {
            "run": str(existing.name),
            "status": str(existing.status),
            "existing": True,
        }
    request_fingerprint = _request_fingerprint(batch, queue, floor, cutoff)
    run = frappe.get_doc(
        {
            "doctype": RUN_DOCTYPE,
            "matching_policy": queue.matching_policy,
            "policy_version": queue.policy_version,
            "run_purpose": RUN_PURPOSE,
            "policy_snapshot_json": queue.policy_snapshot_json,
            "status": "Profiling",
            "approval_status": "Pending Management Review",
            "snapshot_at": queue.snapshot_at,
            "sample_size": HYBRID_VALIDATION_SIZE,
            "double_review_count": HYBRID_VALIDATION_SIZE,
            "candidate_truncated": 0,
            "sampled_pair_count": 0,
            "model_versions_json": _queue_model_versions(queue),
            "validation_queue_run": queue.name,
            "validation_canary_run": queue.canary_run,
            "frozen_automatic_cutoff": cutoff,
            "runtime_versions_json": _queue_runtime_versions(queue),
            "authorization_fingerprint": request_fingerprint,
            "validation_target_count": HYBRID_VALIDATION_SIZE,
            "holdout_target_count": HYBRID_HOLDOUT_SIZE,
            "second_management_approval_status": "Not Requested",
            "hybrid_exploratory_batch": batch.name,
            "hybrid_rule_version": HYBRID_RESEARCH_RULE_VERSION,
            "hybrid_minimum_score": floor,
            "hybrid_route_scope_json": _json(HYBRID_PROPOSAL_ROUTES),
            "hybrid_development_only": 1,
        }
    ).insert(ignore_permissions=True)
    from db_connector.api_identity_resolution import _append_event

    _append_event(
        entity_doctype=RUN_DOCTYPE,
        entity_name=run.name,
        event_type="Create",
        reason="development_hybrid_validation_proposal_requested",
        nonce=request_fingerprint,
        to_status="Profiling",
        metadata={
            "exploratory_batch": batch.name,
            "queue_run": queue.name,
            "development_only": True,
            "identity_writes": 0,
        },
    )
    frappe.db.commit()
    _enqueue(
        "db_connector.api_hybrid_validation.prepare_hybrid_validation_proposal",
        run.name,
    )
    return {"run": run.name, "status": "Profiling", "existing": False}


def prepare_hybrid_validation_proposal(run_name: str) -> None:
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.status != "Profiling":
        return
    try:
        batch, queue, _settings, floor, cutoff = _request_context(
            run.hybrid_exploratory_batch
        )
        inventory = _hybrid_inventory(batch, queue, floor, cutoff)
        profile = _proposal_profile(batch, queue, floor, cutoff, inventory)
        final_fingerprint = hashlib.sha256(
            (
                _request_fingerprint(batch, queue, floor, cutoff)
                + "\x1f"
                + inventory["eligible_manifest_digest"]
                + "\x1f"
                + _json(sorted(inventory["eligible_by_source"]))
            ).encode()
        ).hexdigest()
        frappe.db.set_value(
            RUN_DOCTYPE,
            run.name,
            {
                "status": "Queued",
                "candidate_count": inventory["eligible_count"],
                "record_count": len(
                    {
                        str(record)
                        for row in inventory["rows"]
                        for record in (row["left_record"], row["right_record"])
                    }
                ),
                "profile_json": _json(profile),
                "source_allocation_json": _json(
                    inventory["source_allocations"]
                ),
                "source_scope_json": _json(
                    sorted(inventory["eligible_by_source"])
                ),
                "hybrid_route_allocation_json": _json(
                    inventory["eligible_by_route"]
                ),
                "hybrid_score_band_allocation_json": _json(
                    inventory["eligible_by_score_band"]
                ),
                "hybrid_eligible_manifest_digest": inventory[
                    "eligible_manifest_digest"
                ],
                "hybrid_excluded_exploratory_count": inventory[
                    "exploratory_exposed_count"
                ],
                "authorization_fingerprint": final_fingerprint,
                "error_summary": "",
            },
            update_modified=False,
        )
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            RUN_DOCTYPE,
            run_name,
            {
                "status": "Failed",
                "error_summary": f"hybrid_proposal_failed:{type(exc).__name__}",
            },
            update_modified=False,
        )
        frappe.log_error(
            traceback.format_exc(), "CCD Hybrid Validation Proposal Failure"
        )
        frappe.db.commit()
        raise


@frappe.whitelist(methods=["POST"])
def start_freeze_hybrid_validation(
    run_name: str, deterministic_seed: str, confirm_run_name: str
) -> dict[str, str]:
    _require_manager()
    if str(confirm_run_name or "").strip() != str(run_name or "").strip():
        frappe.throw("Type the exact Validation Run ID to confirm cohort freezing")
    seed = str(deterministic_seed or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", seed):
        frappe.throw("The deterministic seed must be exactly 64 hexadecimal characters")
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.status != "Queued":
        frappe.throw("Only a prepared Hybrid Prospective Validation can be frozen")
    if frappe.db.count(PAIR_DOCTYPE, {"evaluation_run": run.name}):
        frappe.throw("This validation already has frozen Evaluation Pairs")
    if run.validation_seed and seed != str(run.validation_seed):
        frappe.throw(
            "This run already recorded a governance seed; retry with that exact seed"
        )
    run.db_set("validation_seed", seed, update_modified=False)
    run.db_set("status", "Generating Candidates", update_modified=False)
    frappe.db.commit()
    _enqueue(
        "db_connector.api_hybrid_validation.freeze_hybrid_validation_cohorts",
        run.name,
    )
    return {"run": run.name, "status": "Generating Candidates"}


def freeze_hybrid_validation_cohorts(run_name: str) -> None:
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.status != "Generating Candidates":
        return
    try:
        batch, queue, _settings, floor, cutoff = _request_context(
            run.hybrid_exploratory_batch
        )
        inventory = _hybrid_inventory(batch, queue, floor, cutoff)
        if inventory["eligible_manifest_digest"] != str(
            run.hybrid_eligible_manifest_digest or ""
        ):
            frappe.throw(
                "The unseen eligible population changed after proposal preparation; rebuild the proposal"
            )
        prepared_profile = json.loads(run.profile_json or "{}")
        for key in (
            "eligible_by_source",
            "eligible_by_route",
            "eligible_by_score_band",
        ):
            if prepared_profile.get(key) != inventory[key]:
                frappe.throw(
                    "The frozen hybrid population strata changed after proposal preparation; rebuild the proposal"
                )
        cohorts = select_dynamic_hybrid_cohorts(
            inventory["rows"], seed=str(run.validation_seed)
        )
        selected_names = set(cohorts["validation"]) | set(cohorts["holdout"])
        _lock_rows(CANDIDATE_DOCTYPE, selected_names)
        by_name = {str(row["name"]): row for row in inventory["rows"]}
        selected_records = {
            str(record)
            for name in selected_names
            for record in (
                by_name[name]["left_record"],
                by_name[name]["right_record"],
            )
        }
        _lock_rows("CCD Master", selected_records)
        current_states = frappe.get_all(
            CANDIDATE_DOCTYPE,
            filters={"name": ["in", sorted(selected_names)]},
            fields=[
                "name",
                "review_status",
                "final_label",
                "stale",
                "assigned_review_batch",
                "automation_reserved",
                "automation_status",
                "automation_batch",
                "materialization_status",
            ],
            limit_page_length=1_000,
        )
        unavailable = {
            str(row.name): _untouched_reasons(dict(row))
            for row in current_states
            if _untouched_reasons(dict(row))
        }
        if len(current_states) != len(selected_names) or unavailable:
            frappe.throw(
                "One or more selected candidates changed lifecycle after proposal preparation"
            )
        policy = MatchingPolicy.from_dict(json.loads(queue.policy_snapshot_json or "{}"))
        current_records = _load_records(selected_records, policy)
        for name in sorted(selected_names):
            row = by_name[name]
            left = current_records.get(str(row["left_record"]))
            right = current_records.get(str(row["right_record"]))
            if (
                not left
                or not right
                or str(left.get("modified") or "")
                != str(row.get("left_modified_at") or "")
                or str(right.get("modified") or "")
                != str(row.get("right_modified_at") or "")
                or identity_fingerprint(left, policy)
                != str(row.get("left_identity_fingerprint") or "")
                or identity_fingerprint(right, policy)
                != str(row.get("right_identity_fingerprint") or "")
            ):
                frappe.throw(
                    "One or more selected CCD records changed after proposal preparation"
                )
        memberships = _current_membership_records(selected_records)
        exclusions = _active_exclusion_pairs(selected_records)
        if memberships or any(
            tuple(
                sorted(
                    (
                        str(by_name[name]["left_record"]),
                        str(by_name[name]["right_record"]),
                    )
                )
            )
            in exclusions
            for name in selected_names
        ):
            frappe.throw(
                "One or more selected pairs now overlap identity state or a human exclusion"
            )
        if frappe.db.count(PAIR_DOCTYPE, {"evaluation_run": run.name}):
            frappe.throw("This validation already has frozen Evaluation Pairs")

        for candidate_name in cohorts["validation"]:
            row = by_name[str(candidate_name)]
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
                    "double_review_reason": DOUBLE_REVIEW_REASON,
                    "review_status": "Unreviewed",
                    "hybrid_primary_route": row["primary_route"],
                    "hybrid_routes_json": _json(row["routes"]),
                    "hybrid_score_band": row["score_band"],
                    "hybrid_birthday_status": row["birthday_status"],
                    "reason_codes_json": _json(
                        {
                            "development_only": True,
                            "hybrid_rule_version": HYBRID_RESEARCH_RULE_VERSION,
                            "primary_route": row["primary_route"],
                            "routes": row["routes"],
                            "score_band": row["score_band"],
                            "birthday_status": row["birthday_status"],
                        }
                    ),
                }
            ).insert(ignore_permissions=True)

        for cohort, names in (
            ("Validation", cohorts["validation"]),
            ("Blinded Rollout Holdout", cohorts["holdout"]),
        ):
            for candidate_name in names:
                metadata = by_name[str(candidate_name)]["component_metadata"]
                frappe.db.set_value(
                    CANDIDATE_DOCTYPE,
                    candidate_name,
                    {
                        "automation_validation_run": run.name,
                        "automation_cohort": cohort,
                        "automation_reserved": 1,
                        "automation_component_fingerprint": metadata[
                            "component_fingerprint"
                        ],
                        "automation_component_size": metadata["component_size"],
                        "automation_complete_clique": int(
                            metadata["complete_clique"]
                        ),
                        "automation_status": (
                            "Reserved Validation"
                            if cohort == "Validation"
                            else "Reserved Holdout"
                        ),
                        "assigned_review_batch": None,
                        "assigned_to": None,
                        "assigned_at": None,
                        "due_at": None,
                    },
                    update_modified=False,
                )

        allocation = {
            "available": inventory["eligible_by_source"],
            **cohorts["source_allocations"],
        }
        frappe.db.set_value(
            RUN_DOCTYPE,
            run.name,
            {
                "status": "Reviewing",
                "sampled_pair_count": HYBRID_VALIDATION_SIZE,
                "holdout_eligible_count": HYBRID_HOLDOUT_SIZE,
                "source_allocation_json": _json(allocation),
                "error_summary": "",
            },
            update_modified=False,
        )
        from db_connector.api_identity_resolution import _append_event

        _append_event(
            entity_doctype=RUN_DOCTYPE,
            entity_name=run.name,
            event_type="Revalidate",
            reason="development_hybrid_validation_and_holdout_frozen",
            nonce=hashlib.sha256(
                f"{run.name}\x1f{run.validation_seed}\x1f{run.hybrid_eligible_manifest_digest}".encode()
            ).hexdigest(),
            from_status="Queued",
            to_status="Reviewing",
            metadata={
                "development_only": True,
                "identity_writes": 0,
                "validation": HYBRID_VALIDATION_SIZE,
                "holdout": HYBRID_HOLDOUT_SIZE,
                "source_allocation": allocation,
            },
        )
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            RUN_DOCTYPE,
            run_name,
            {
                "status": "Failed",
                "error_summary": f"hybrid_cohort_freeze_failed:{type(exc).__name__}",
            },
            update_modified=False,
        )
        frappe.log_error(
            traceback.format_exc(), "CCD Hybrid Validation Cohort Freeze Failure"
        )
        frappe.db.commit()
        raise


def _provenance_reasons(run: Any) -> list[str]:
    reasons: list[str] = []
    if run.run_purpose != RUN_PURPOSE or not run.hybrid_development_only:
        reasons.append("wrong_or_non_development_run")
        return reasons
    try:
        batch, queue, settings, floor, cutoff = _request_context(
            run.hybrid_exploratory_batch
        )
        if str(queue.name) != str(run.validation_queue_run):
            reasons.append("queue_changed")
        if str(queue.canary_run) != str(run.validation_canary_run):
            reasons.append("canary_changed")
        if str(queue.policy_snapshot_json or "") != str(run.policy_snapshot_json or ""):
            reasons.append("policy_snapshot_changed")
        if _queue_model_versions(queue) != str(run.model_versions_json or "{}"):
            reasons.append("model_versions_changed")
        if _queue_runtime_versions(queue) != str(run.runtime_versions_json or "{}"):
            reasons.append("runtime_versions_changed")
        if HYBRID_RESEARCH_RULE_VERSION != str(run.hybrid_rule_version or ""):
            reasons.append("hybrid_rule_version_changed")
        if abs(float(run.hybrid_minimum_score or 0) - floor) > 1e-12:
            reasons.append("research_floor_changed")
        if abs(float(run.frozen_automatic_cutoff or 0) - cutoff) > 1e-12:
            reasons.append("ultra_high_cutoff_changed")
        if tuple(json.loads(run.hybrid_route_scope_json or "[]")) != tuple(
            HYBRID_PROPOSAL_ROUTES
        ):
            reasons.append("route_scope_changed")
        if str(batch.eligible_manifest_digest or "") == "":
            reasons.append("exploratory_manifest_missing")
        if str(settings.authorized_splink_queue or "") != str(queue.name):
            reasons.append("authorized_queue_changed")
    except Exception as exc:
        reasons.append(f"provenance_unavailable:{type(exc).__name__}")
    return sorted(set(reasons))


def _validation_pairs(run_name: str) -> list[Any]:
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
            "source_pair",
            "hybrid_primary_route",
            "hybrid_score_band",
        ],
        limit_page_length=1_000,
    )


def _pair_stale(row: Any) -> bool:
    current_left = frappe.db.get_value("CCD Master", row.left_record, "modified")
    current_right = frappe.db.get_value("CCD Master", row.right_record, "modified")
    return (
        not current_left
        or not current_right
        or frappe.utils.get_datetime(current_left)
        != frappe.utils.get_datetime(row.left_modified_at)
        or frappe.utils.get_datetime(current_right)
        != frappe.utils.get_datetime(row.right_modified_at)
    )


def _subgroup_metrics(rows: list[Any]) -> dict[str, Any]:
    dimensions = {
        "source_pair": "source_pair",
        "primary_route": "hybrid_primary_route",
        "score_band": "hybrid_score_band",
    }
    output: dict[str, Any] = {}
    for label, fieldname in dimensions.items():
        groups: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            if row.stale or row.final_label not in {"Same", "Different"}:
                continue
            groups[str(getattr(row, fieldname) or "Unknown")].append(
                str(row.final_label)
            )
        output[label] = {
            key: validation_gate(values, minimum_valid=1, lower_target=0.0)
            for key, values in sorted(groups.items())
        }
    return output


@frappe.whitelist(methods=["POST"])
def finalize_hybrid_prospective_validation(run_name: str) -> dict[str, Any]:
    _require_manager()
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.status not in {
        "Reviewing",
        "Awaiting Management Approval",
    }:
        frappe.throw("Only a reviewing Hybrid Prospective Validation can be finalized")
    provenance_reasons = _provenance_reasons(run)
    if provenance_reasons:
        frappe.db.set_value(
            RUN_DOCTYPE,
            run.name,
            {
                "authorization_stale": 1,
                "authorization_stale_reason": ",".join(provenance_reasons)[:140],
                "status": "Stale",
            },
            update_modified=False,
        )
        frappe.db.commit()
        frappe.throw("Hybrid validation provenance is stale: " + ", ".join(provenance_reasons))

    rows = _validation_pairs(run.name)
    stale = 0
    unresolved = 0
    labels: list[str] = []
    for row in rows:
        if row.stale or _pair_stale(row):
            stale += 1
            if not row.stale:
                frappe.db.set_value(
                    PAIR_DOCTYPE, row.name, "stale", 1, update_modified=False
                )
                row.stale = 1
            continue
        ordinary_reviewers = {
            str(value)
            for value in frappe.get_all(
                LABEL_DOCTYPE,
                filters={
                    "parent": row.name,
                    "parenttype": PAIR_DOCTYPE,
                    "is_adjudication": 0,
                },
                pluck="reviewer",
                limit_page_length=10,
            )
        }
        if (
            row.review_status not in FINAL_REVIEW_STATUSES
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
                "run_purpose": RUN_PURPOSE,
                "development_only": True,
                "authorizes_identity_writes": False,
                "accepted_global_population_risk": True,
                "subgroup_precision": _subgroup_metrics(rows),
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
                (
                    "Validation Final"
                    if row.final_label in {"Same", "Different"} and not row.stale
                    else "Stale"
                ),
                update_modified=False,
            )
    frappe.db.commit()
    return {"run": run.name, "status": values["status"], **gate}


@frappe.whitelist(methods=["POST"])
def record_hybrid_development_decision(
    run_name: str, decision: str, reason: str, confirm_run_name: str
) -> dict[str, str]:
    _require_manager()
    if str(confirm_run_name or "").strip() != str(run_name or "").strip():
        frappe.throw("Type the exact Validation Run ID to confirm the decision")
    if decision not in {"Approved", "Rejected"}:
        frappe.throw("Decision must be Approved or Rejected")
    if not str(reason or "").strip():
        frappe.throw("A management reason is required")
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.status != "Awaiting Management Approval":
        frappe.throw("This hybrid validation is not awaiting a Development decision")
    if decision == "Approved" and _provenance_reasons(run):
        frappe.throw("The frozen Development validation provenance is stale")
    metrics = json.loads(run.metrics_json or "{}")
    gate = metrics.get("authorization_gate") or {}
    if decision == "Approved" and not gate.get("passed"):
        frappe.throw("The prospective precision gate did not pass")
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {"status": "Completed", "approval_status": decision},
        update_modified=False,
    )
    from db_connector.api_identity_resolution import _append_event

    _append_event(
        entity_doctype=RUN_DOCTYPE,
        entity_name=run.name,
        event_type="Approve" if decision == "Approved" else "Withdraw",
        reason="development_only_hybrid_evidence:" + str(reason).strip(),
        nonce=hashlib.sha256(
            f"{run.name}\x1f{decision}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Awaiting Management Approval",
        to_status=decision,
        metadata={
            "development_only": True,
            "authorizes_identity_writes": False,
            "gate": gate,
        },
    )
    frappe.db.commit()
    return {"run": run.name, "status": "Completed", "approval_status": decision}


@frappe.whitelist()
def get_next_hybrid_validation_pair(run_name: str) -> dict[str, str]:
    _require_reviewer()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.status != "Reviewing":
        frappe.throw("The Hybrid Prospective Validation is not open for review")
    rows = frappe.get_all(
        PAIR_DOCTYPE,
        filters={
            "evaluation_run": run.name,
            "automation_cohort": "Validation",
            "stale": 0,
            "review_status": [
                "in",
                ["Unreviewed", "Partially Reviewed", "Positive Confirmation Required"],
            ],
        },
        fields=["name", "creation"],
        order_by="creation asc, name asc",
        limit_page_length=1_000,
    )
    if not rows:
        return {"run": run.name, "pair": ""}
    names = [str(row.name) for row in rows]
    already_reviewed = {
        str(parent)
        for parent in frappe.get_all(
            LABEL_DOCTYPE,
            filters={
                "parent": ["in", names],
                "parenttype": PAIR_DOCTYPE,
                "reviewer": frappe.session.user,
                "is_adjudication": 0,
            },
            pluck="parent",
            limit_page_length=1_000,
        )
    }
    pair_name = next((name for name in names if name not in already_reviewed), "")
    return {"run": run.name, "pair": pair_name}


@frappe.whitelist(methods=["POST"])
def retry_hybrid_validation_job(run_name: str) -> dict[str, str]:
    _require_manager()
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.status != "Failed":
        frappe.throw("Only a failed Hybrid Prospective Validation job can be retried")
    if frappe.db.count(PAIR_DOCTYPE, {"evaluation_run": run.name}):
        frappe.throw("A failed run with frozen Evaluation Pairs requires investigation")
    # Rebuild the exact eligible manifest after either a proposal or freeze
    # failure.  A previously recorded seed is deliberately retained and the
    # freeze endpoint rejects any replacement seed.
    status = "Profiling"
    method = "db_connector.api_hybrid_validation.prepare_hybrid_validation_proposal"
    run.db_set("status", status, update_modified=False)
    run.db_set("error_summary", "", update_modified=False)
    frappe.db.commit()
    _enqueue(method, run.name)
    return {"run": run.name, "status": status}


def _reserved_holdout_candidates(run: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in frappe.get_all(
            CANDIDATE_DOCTYPE,
            filters={
                "automation_validation_run": run.name,
                "automation_cohort": "Blinded Rollout Holdout",
                "automation_reserved": 1,
            },
            fields=[
                "name",
                "queue_run",
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
                "review_status",
                "final_label",
                "stale",
                "assigned_review_batch",
                "automation_status",
                "automation_batch",
                "automation_component_fingerprint",
                "automation_component_size",
                "automation_complete_clique",
                "materialization_status",
            ],
            order_by="name asc",
            limit_page_length=1_000,
        )
    ]


def _holdout_current_eligibility(
    run: Any, rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    queue = frappe.get_doc(QUEUE_DOCTYPE, run.validation_queue_run)
    policy = MatchingPolicy.from_dict(json.loads(queue.policy_snapshot_json or "{}"))
    record_ids = {
        str(record)
        for row in rows
        for record in (row.get("left_record"), row.get("right_record"))
        if record
    }
    records = _load_records(record_ids, policy)
    memberships = _current_membership_records(record_ids)
    exclusions = _active_exclusion_pairs(record_ids)
    allowed_routes = set(HYBRID_PROPOSAL_ROUTES)
    eligible: list[dict[str, Any]] = []
    ineligible: dict[str, list[str]] = {}

    for row in rows:
        reasons: list[str] = []
        left_id = str(row.get("left_record") or "")
        right_id = str(row.get("right_record") or "")
        left = records.get(left_id)
        right = records.get(right_id)
        if str(row.get("queue_run") or "") != str(run.validation_queue_run):
            reasons.append("queue_changed")
        if row.get("stale"):
            reasons.append("candidate_marked_stale")
        if str(row.get("review_status") or "") != "Unreviewed" or row.get("final_label"):
            reasons.append("prior_human_review")
        if row.get("assigned_review_batch"):
            reasons.append("assigned_optional_review")
        if str(row.get("automation_status") or "") != "Reserved Holdout":
            reasons.append("holdout_reservation_changed")
        if row.get("automation_batch"):
            reasons.append("automation_batch_started")
        if str(row.get("materialization_status") or "Not Final") != "Not Final":
            reasons.append("materialization_started")
        score = float(row.get("probabilistic_score") or 0)
        if not float(run.hybrid_minimum_score or 0) <= score < float(
            run.frozen_automatic_cutoff or 0
        ):
            reasons.append("outside_frozen_score_range")
        expected_component = hashlib.sha256(
            "\x1f".join(sorted((left_id, right_id))).encode()
        ).hexdigest()
        if (
            int(row.get("automation_component_size") or 0) != 2
            or not int(row.get("automation_complete_clique") or 0)
            or str(row.get("automation_component_fingerprint") or "")
            != expected_component
        ):
            reasons.append("frozen_component_not_isolated_pair")
        if not left or not right:
            reasons.append("record_missing")
        else:
            if str(left.get("source") or "") == str(right.get("source") or ""):
                reasons.append("same_source")
            if (
                str(left.get("modified") or "")
                != str(row.get("left_modified_at") or "")
                or str(right.get("modified") or "")
                != str(row.get("right_modified_at") or "")
            ):
                reasons.append("source_modified_after_freeze")
            if (
                identity_fingerprint(left, policy)
                != str(row.get("left_identity_fingerprint") or "")
                or identity_fingerprint(right, policy)
                != str(row.get("right_identity_fingerprint") or "")
            ):
                reasons.append("identity_fingerprint_changed")
            classification = _classify_pair(left, right, policy)
            if classification["hard_conflicts"]:
                reasons.extend(str(item) for item in classification["hard_conflicts"])
            routes = tuple(sorted(allowed_routes.intersection(classification["routes"])))
            if not routes:
                reasons.append("outside_frozen_route_scope")
            else:
                row["routes"] = routes
                row["primary_route"] = (
                    "exact_name+unverified_identifier_exact"
                    if "exact_name+unverified_identifier_exact" in routes
                    else "name_support+unverified_identifier+secondary_exact"
                )
                row["score_band"] = lower_score_band(
                    score, float(run.frozen_automatic_cutoff or 0)
                )
                row["birthday_status"] = str(classification["birthday_status"])
        if left_id in memberships or right_id in memberships:
            reasons.append("existing_group_overlap")
        if tuple(sorted((left_id, right_id))) in exclusions:
            reasons.append("active_human_exclusion")
        if reasons:
            ineligible[str(row["name"])] = sorted(set(reasons))
        else:
            row["source_pair"] = canonical_source_pair(row.get("source_pair"))
            eligible.append(row)
    return eligible, dict(sorted(ineligible.items()))


def _holdout_allocation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source_pair": dict(sorted(Counter(str(row["source_pair"]) for row in rows).items())),
        "primary_route": dict(sorted(Counter(str(row["primary_route"]) for row in rows).items())),
        "score_band": dict(sorted(Counter(str(row["score_band"]) for row in rows).items())),
        "birthday_status": dict(sorted(Counter(str(row["birthday_status"]) for row in rows).items())),
    }


@frappe.whitelist(methods=["POST"])
def start_hybrid_holdout_qc(run_name: str) -> dict[str, str]:
    """Queue current-state checks for the exact already-reserved holdout."""
    _require_manager()
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    status = str(run.hybrid_holdout_status or "Not Created")
    if (
        run.run_purpose != RUN_PURPOSE
        or run.status != "Completed"
        or run.approval_status != "Approved"
        or not run.hybrid_development_only
    ):
        frappe.throw("Approve the Development hybrid validation before creating holdout QC")
    if status in {"Building", "Reviewing", "Awaiting Management Approval", "Completed"}:
        return {"run": run.name, "status": status, "existing": "1"}
    if status not in {"Not Created", "Failed"}:
        frappe.throw("This Development holdout cannot be created in its current state")
    if _provenance_reasons(run):
        frappe.throw("The frozen Development validation provenance is stale")
    metrics = json.loads(run.metrics_json or "{}")
    if not (metrics.get("authorization_gate") or {}).get("passed"):
        frappe.throw("The approved prospective validation precision gate is missing")
    if frappe.db.count(
        PAIR_DOCTYPE,
        {"evaluation_run": run.name, "automation_cohort": "Blinded Rollout Holdout"},
    ):
        frappe.throw("This run already has persistent holdout Evaluation Pairs")
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            "hybrid_holdout_status": "Building",
            "hybrid_holdout_error": "",
            "hybrid_second_approval_status": "Not Requested",
        },
        update_modified=False,
    )
    frappe.db.commit()
    _enqueue("db_connector.api_hybrid_validation.build_hybrid_holdout_qc", run.name)
    return {"run": run.name, "status": "Building", "existing": "0"}


def build_hybrid_holdout_qc(run_name: str) -> None:
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.hybrid_holdout_status != "Building":
        return
    try:
        provenance_reasons = _provenance_reasons(run)
        if provenance_reasons:
            frappe.throw("Frozen provenance changed: " + ", ".join(provenance_reasons))
        reserved = _reserved_holdout_candidates(run)
        if len(reserved) != HYBRID_HOLDOUT_SIZE:
            frappe.throw(
                f"The exact frozen holdout contains {len(reserved)} reserved pairs; "
                f"{HYBRID_HOLDOUT_SIZE} required"
            )
        _lock_rows(CANDIDATE_DOCTYPE, [row["name"] for row in reserved])
        _lock_rows(
            "CCD Master",
            [
                str(record)
                for row in reserved
                for record in (row["left_record"], row["right_record"])
            ],
        )
        eligible, ineligible = _holdout_current_eligibility(run, reserved)
        if len(eligible) < HYBRID_MINIMUM_ELIGIBLE_HOLDOUT:
            frappe.db.set_value(
                RUN_DOCTYPE,
                run.name,
                {
                    "hybrid_holdout_status": "Failed",
                    "holdout_eligible_count": len(eligible),
                    "hybrid_holdout_ineligibility_json": _json(ineligible),
                    "hybrid_holdout_error": (
                        f"eligible_holdout_below_floor:{len(eligible)}:"
                        f"{HYBRID_MINIMUM_ELIGIBLE_HOLDOUT}"
                    ),
                },
                update_modified=False,
            )
            frappe.db.commit()
            return
        if frappe.db.count(
            PAIR_DOCTYPE,
            {"evaluation_run": run.name, "automation_cohort": "Blinded Rollout Holdout"},
        ):
            frappe.throw("This run already has persistent holdout Evaluation Pairs")
        for row in sorted(eligible, key=lambda item: str(item["name"])):
            frappe.get_doc(
                {
                    "doctype": PAIR_DOCTYPE,
                    "evaluation_run": run.name,
                    "review_candidate": row["name"],
                    "automation_cohort": "Blinded Rollout Holdout",
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
                    "double_review_reason": HOLDOUT_DOUBLE_REVIEW_REASON,
                    "review_status": "Unreviewed",
                    "hybrid_primary_route": row["primary_route"],
                    "hybrid_routes_json": _json(row["routes"]),
                    "hybrid_score_band": row["score_band"],
                    "hybrid_birthday_status": row["birthday_status"],
                    "reason_codes_json": _json(
                        {
                            "development_only": True,
                            "zero_identity_writes": True,
                            "hybrid_rule_version": HYBRID_RESEARCH_RULE_VERSION,
                            "primary_route": row["primary_route"],
                            "routes": row["routes"],
                            "score_band": row["score_band"],
                            "birthday_status": row["birthday_status"],
                        }
                    ),
                }
            ).insert(ignore_permissions=True)
        manifest = _eligible_manifest_digest(eligible)
        allocation = _holdout_allocation(eligible)
        frappe.db.set_value(
            RUN_DOCTYPE,
            run.name,
            {
                "hybrid_holdout_status": "Reviewing",
                "hybrid_holdout_manifest_digest": manifest,
                "hybrid_holdout_allocation_json": _json(allocation),
                "hybrid_holdout_ineligibility_json": _json(ineligible),
                "holdout_eligible_count": len(eligible),
                "holdout_qc_complete_count": 0,
                "holdout_qc_same_count": 0,
                "holdout_qc_different_count": 0,
                "hybrid_holdout_metrics_json": "",
                "hybrid_holdout_error": "",
                "hybrid_second_approval_status": "Not Requested",
            },
            update_modified=False,
        )
        from db_connector.api_identity_resolution import _append_event

        _append_event(
            entity_doctype=RUN_DOCTYPE,
            entity_name=run.name,
            event_type="QC Assign",
            reason="development_hybrid_blinded_holdout_qc_created",
            nonce=manifest,
            from_status="Approved Development Validation",
            to_status="Holdout Reviewing",
            metadata={
                "development_only": True,
                "identity_writes": 0,
                "reserved_count": len(reserved),
                "eligible_count": len(eligible),
                "ineligible_count": len(ineligible),
                "allocation": allocation,
            },
        )
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            RUN_DOCTYPE,
            run_name,
            {
                "hybrid_holdout_status": "Failed",
                "hybrid_holdout_error": f"hybrid_holdout_build_failed:{type(exc).__name__}",
            },
            update_modified=False,
        )
        frappe.log_error(traceback.format_exc(), "CCD Hybrid Holdout Build Failure")
        frappe.db.commit()
        raise


def _cohort_pairs(run_name: str, cohort: str) -> list[Any]:
    return frappe.get_all(
        PAIR_DOCTYPE,
        filters={"evaluation_run": run_name, "automation_cohort": cohort},
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
            "source_pair",
            "hybrid_primary_route",
            "hybrid_score_band",
        ],
        order_by="creation asc, name asc",
        limit_page_length=1_000,
    )


def _reviewed_cohort(rows: list[Any]) -> dict[str, Any]:
    pair_names = [str(row.name) for row in rows]
    labels_by_pair: dict[str, set[str]] = defaultdict(set)
    if pair_names:
        for label in frappe.get_all(
            LABEL_DOCTYPE,
            filters={
                "parent": ["in", pair_names],
                "parenttype": PAIR_DOCTYPE,
                "is_adjudication": 0,
            },
            fields=["parent", "reviewer"],
            limit_page_length=5_000,
        ):
            labels_by_pair[str(label.parent)].add(str(label.reviewer))
    stale = 0
    unresolved = 0
    final_labels: list[str] = []
    valid_rows: list[Any] = []
    for row in rows:
        if row.stale or _pair_stale(row):
            stale += 1
            if not row.stale:
                frappe.db.set_value(PAIR_DOCTYPE, row.name, "stale", 1, update_modified=False)
                row.stale = 1
            continue
        if (
            row.review_status not in FINAL_REVIEW_STATUSES
            or row.final_label not in {"Same", "Different"}
            or len(labels_by_pair[str(row.name)]) < 2
        ):
            unresolved += 1
            continue
        final_labels.append(str(row.final_label))
        valid_rows.append(row)
    return {
        "labels": final_labels,
        "rows": valid_rows,
        "stale": stale,
        "unresolved": unresolved,
    }


@frappe.whitelist(methods=["POST"])
def finalize_hybrid_holdout_qc(run_name: str) -> dict[str, Any]:
    _require_manager()
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.hybrid_holdout_status not in {
        "Reviewing",
        "Awaiting Management Approval",
    }:
        frappe.throw("Only a reviewing Development hybrid holdout can be finalized")
    provenance_reasons = _provenance_reasons(run)
    if provenance_reasons:
        frappe.db.set_value(
            RUN_DOCTYPE,
            run.name,
            {
                "hybrid_holdout_status": "Stale",
                "hybrid_holdout_error": ",".join(provenance_reasons)[:140],
                "authorization_stale": 1,
                "authorization_stale_reason": ",".join(provenance_reasons)[:140],
            },
            update_modified=False,
        )
        frappe.db.commit()
        frappe.throw("Hybrid holdout provenance is stale: " + ", ".join(provenance_reasons))
    validation = _reviewed_cohort(_cohort_pairs(run.name, "Validation"))
    holdout = _reviewed_cohort(
        _cohort_pairs(run.name, "Blinded Rollout Holdout")
    )
    if validation["unresolved"] or holdout["unresolved"]:
        frappe.throw(
            "Finish every independent review and required adjudication before finalizing "
            f"(validation unresolved: {validation['unresolved']}; "
            f"holdout unresolved: {holdout['unresolved']})"
        )
    gates = hybrid_holdout_gate(
        validation["labels"],
        holdout["labels"],
        validation_stale_count=validation["stale"],
        validation_unresolved_count=validation["unresolved"],
        holdout_stale_count=holdout["stale"],
        holdout_unresolved_count=holdout["unresolved"],
    )
    combined_rows = validation["rows"] + holdout["rows"]
    metrics = {
        "run_purpose": RUN_PURPOSE,
        "development_only": True,
        "authorizes_identity_writes": False,
        "reserved_holdout_target": HYBRID_HOLDOUT_SIZE,
        "eligible_holdout": int(run.holdout_eligible_count or 0),
        "stale_counts": {
            "validation": validation["stale"],
            "holdout": holdout["stale"],
        },
        "unresolved_counts": {
            "validation": validation["unresolved"],
            "holdout": holdout["unresolved"],
        },
        "validation": validation_gate(
            validation["labels"],
            stale_count=validation["stale"],
            unresolved_count=validation["unresolved"],
        ),
        "holdout": gates["holdout"],
        "combined_authorization_gate": gates["combined"],
        "holdout_subgroup_precision": _subgroup_metrics(holdout["rows"]),
        "combined_subgroup_precision": _subgroup_metrics(combined_rows),
    }
    passed = bool(gates["combined"]["passed"])
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            "hybrid_holdout_status": (
                "Awaiting Management Approval" if passed else "Completed"
            ),
            "holdout_qc_complete_count": gates["holdout"]["valid"],
            "holdout_qc_same_count": gates["holdout"]["same"],
            "holdout_qc_different_count": gates["holdout"]["different"],
            "hybrid_holdout_metrics_json": _json(metrics),
            "hybrid_holdout_error": (
                "" if passed else ";".join(gates["combined"]["reasons"])[:140]
            ),
            "hybrid_second_approval_status": (
                "Pending Management Review" if passed else "Rejected"
            ),
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "run": run.name,
        "status": "Awaiting Management Approval" if passed else "Completed",
        **gates,
    }


@frappe.whitelist(methods=["POST"])
def record_hybrid_holdout_development_decision(
    run_name: str, decision: str, reason: str, confirm_run_name: str
) -> dict[str, str]:
    _require_manager()
    if str(confirm_run_name or "").strip() != str(run_name or "").strip():
        frappe.throw("Type the exact Validation Run ID to confirm the holdout decision")
    if decision not in {"Approved", "Rejected"}:
        frappe.throw("Decision must be Approved or Rejected")
    if not str(reason or "").strip():
        frappe.throw("A Development holdout decision reason is required")
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if (
        run.run_purpose != RUN_PURPOSE
        or run.hybrid_holdout_status != "Awaiting Management Approval"
    ):
        frappe.throw("This hybrid holdout is not awaiting a Development decision")
    metrics = json.loads(run.hybrid_holdout_metrics_json or "{}")
    gate = metrics.get("combined_authorization_gate") or {}
    if decision == "Approved":
        if _provenance_reasons(run):
            frappe.throw("The frozen Development holdout provenance is stale")
        if not gate.get("passed"):
            frappe.throw("The combined validation and holdout precision gate did not pass")
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            "hybrid_holdout_status": "Completed",
            "hybrid_second_approval_status": decision,
            "hybrid_second_approved_at": frappe.utils.now_datetime(),
            "hybrid_second_approved_by": frappe.session.user,
        },
        update_modified=False,
    )
    from db_connector.api_identity_resolution import _append_event

    _append_event(
        entity_doctype=RUN_DOCTYPE,
        entity_name=run.name,
        event_type="Approve" if decision == "Approved" else "Withdraw",
        reason="development_only_hybrid_holdout:" + str(reason).strip(),
        nonce=hashlib.sha256(
            f"{run.name}\x1fholdout\x1f{decision}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Holdout Awaiting Management Approval",
        to_status=decision,
        metadata={
            "development_only": True,
            "authorizes_identity_writes": False,
            "combined_gate": gate,
        },
    )
    frappe.db.commit()
    return {"run": run.name, "status": "Completed", "decision": decision}


@frappe.whitelist()
def get_next_hybrid_holdout_pair(run_name: str) -> dict[str, str]:
    _require_reviewer()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.hybrid_holdout_status != "Reviewing":
        frappe.throw("The Development hybrid holdout is not open for review")
    rows = frappe.get_all(
        PAIR_DOCTYPE,
        filters={
            "evaluation_run": run.name,
            "automation_cohort": "Blinded Rollout Holdout",
            "stale": 0,
            "review_status": [
                "in",
                ["Unreviewed", "Partially Reviewed", "Positive Confirmation Required"],
            ],
        },
        fields=["name", "creation"],
        order_by="creation asc, name asc",
        limit_page_length=1_000,
    )
    names = [str(row.name) for row in rows]
    already_reviewed = {
        str(parent)
        for parent in frappe.get_all(
            LABEL_DOCTYPE,
            filters={
                "parent": ["in", names],
                "parenttype": PAIR_DOCTYPE,
                "reviewer": frappe.session.user,
                "is_adjudication": 0,
            },
            pluck="parent",
            limit_page_length=1_000,
        )
    } if names else set()
    pair_name = next((name for name in names if name not in already_reviewed), "")
    return {"run": run.name, "pair": pair_name}


@frappe.whitelist()
def get_next_hybrid_review_pair(pair_name: str) -> dict[str, str]:
    _require_reviewer()
    pair = frappe.get_doc(PAIR_DOCTYPE, pair_name)
    if pair.automation_cohort == "Blinded Rollout Holdout":
        return get_next_hybrid_holdout_pair(pair.evaluation_run)
    return get_next_hybrid_validation_pair(pair.evaluation_run)


@frappe.whitelist(methods=["POST"])
def retry_hybrid_holdout_job(run_name: str) -> dict[str, str]:
    _require_manager()
    _lock_rows(RUN_DOCTYPE, (run_name,))
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.run_purpose != RUN_PURPOSE or run.hybrid_holdout_status != "Failed":
        frappe.throw("Only a failed Development hybrid holdout build can be retried")
    if frappe.db.count(
        PAIR_DOCTYPE,
        {"evaluation_run": run.name, "automation_cohort": "Blinded Rollout Holdout"},
    ):
        frappe.throw("A failed holdout with persistent pairs requires investigation")
    run.db_set("hybrid_holdout_status", "Building", update_modified=False)
    run.db_set("hybrid_holdout_error", "", update_modified=False)
    frappe.db.commit()
    _enqueue("db_connector.api_hybrid_validation.build_hybrid_holdout_qc", run.name)
    return {"run": run.name, "status": "Building"}


@frappe.whitelist()
def get_hybrid_holdout_dashboard() -> dict[str, Any]:
    _require_manager()
    rows = frappe.get_all(
        RUN_DOCTYPE,
        filters={
            "run_purpose": RUN_PURPOSE,
            "status": "Completed",
            "approval_status": "Approved",
            "hybrid_development_only": 1,
        },
        fields=["name"],
        order_by="creation desc",
        limit_page_length=1,
    )
    if not rows:
        return {
            "available": False,
            "message": "No approved Development hybrid prospective validation exists.",
        }
    run = frappe.get_doc(RUN_DOCTYPE, rows[0].name)
    holdout_status = str(run.hybrid_holdout_status or "Not Created")
    metrics = json.loads(run.hybrid_holdout_metrics_json or "{}")
    allocation = json.loads(run.hybrid_holdout_allocation_json or "{}")
    ineligibility = json.loads(run.hybrid_holdout_ineligibility_json or "{}")
    return {
        "available": True,
        "run": run.name,
        "status": holdout_status,
        "second_approval_status": str(
            run.hybrid_second_approval_status or "Not Requested"
        ),
        "reserved_target": int(run.holdout_target_count or HYBRID_HOLDOUT_SIZE),
        "eligible": int(run.holdout_eligible_count or 0),
        "complete": int(run.holdout_qc_complete_count or 0),
        "same": int(run.holdout_qc_same_count or 0),
        "different": int(run.holdout_qc_different_count or 0),
        "ineligible": len(ineligibility),
        "allocation": allocation,
        "metrics": metrics,
        "error": str(run.hybrid_holdout_error or ""),
        "can_create": holdout_status == "Not Created",
        "can_retry": holdout_status == "Failed",
        "can_review": holdout_status == "Reviewing",
        "can_finalize": holdout_status == "Reviewing",
        "can_decide": holdout_status == "Awaiting Management Approval",
        "zero_identity_writes": True,
        "development_only": True,
    }
