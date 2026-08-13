"""Reversible recommendation-only canary for the validated CCD High rule.

This module writes only dedicated canary DocTypes. It never merges CCD Master
records, changes ``Is Matched?``, or writes the production matching child table.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from collections import Counter
from typing import Any

import frappe

from db_connector.api_fuzzy_evaluation import (
    DEFAULT_PILOT_POLICY_VERSION,
    HIGH_TIER_VALIDATION,
    THRESHOLD_EVALUATION,
    _canonical_record,
    _policy_from_doc,
    _policy_snapshot,
)
from db_connector.fuzzy_matching import normalization as norm
from db_connector.fuzzy_matching.blocking import BLOCKING_VERSION, generate_candidate_pairs
from db_connector.fuzzy_matching.canary import CanaryEdge, analyze_canary_edges, ordered_pair
from db_connector.fuzzy_matching.models import build_evidence, tiered_result
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.types import MatchTier

RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
EVENT_DOCTYPE = "CCD Match Recommendation Event"
APPROVED_HIGH_REASON = "exact_name_plus_independent_evidence"
RUNNING_STATUSES = (
    "Queued",
    "Profiling",
    "Generating Candidates",
    "Applying Safety Gates",
    "Writing Recommendations",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _snapshot_hash(value: str | dict[str, Any]) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value
    canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _pair_fingerprint(policy_version: str, left_id: str, right_id: str) -> str:
    left, right = ordered_pair(left_id, right_id)
    return hashlib.sha256(f"{policy_version}\x1f{left}\x1f{right}".encode()).hexdigest()


def _recommendation_key(run_name: str, left_id: str, right_id: str) -> str:
    left, right = ordered_pair(left_id, right_id)
    return hashlib.sha256(f"{run_name}\x1f{left}\x1f{right}".encode()).hexdigest()


def _approved_run(
    policy_name: str,
    run_purpose: str,
    snapshot_sha256: str,
) -> Any:
    rows = frappe.get_all(
        "CCD Match Evaluation Run",
        filters={
            "matching_policy": policy_name,
            "run_purpose": run_purpose,
            "status": "Completed",
            "approval_status": "Approved",
        },
        fields=["name", "policy_snapshot_json", "metrics_json", "modified"],
        order_by="modified desc",
        limit=20,
    )
    for row in rows:
        if _snapshot_hash(row.policy_snapshot_json) == snapshot_sha256:
            row.metrics = json.loads(row.metrics_json or "{}")
            return row
    frappe.throw(
        f"No approved {run_purpose} uses the current frozen policy snapshot"
    )


def _canary_prerequisites(policy_name: str) -> dict[str, Any]:
    policy_doc = frappe.get_doc("CCD Matching Policy", policy_name)
    policy = _policy_from_doc(policy_doc)
    snapshot = _policy_snapshot(policy)
    snapshot_sha256 = _snapshot_hash(snapshot)
    high_run = _approved_run(policy_name, HIGH_TIER_VALIDATION, snapshot_sha256)
    threshold_run = _approved_run(policy_name, THRESHOLD_EVALUATION, snapshot_sha256)

    high_metrics = high_run.metrics.get("high_tier_validation") or {}
    lower = list(high_metrics.get("precision_wilson_95") or [0])[0]
    if (
        not high_metrics.get("all_sampled_pairs_were_high")
        or int(high_metrics.get("sampled_predictions") or 0) < policy.minimum_high_samples
        or float(lower or 0) < policy.high_precision_target
    ):
        frappe.throw("The approved High validation no longer meets the policy safeguards")

    splink = (threshold_run.metrics.get("models") or {}).get(
        "fellegi_sunter_calibration"
    ) or {}
    review_threshold = splink.get("review_threshold")
    if not splink.get("validation_ready") or review_threshold is None:
        frappe.throw("The approved threshold evaluation has no valid Review threshold")

    validated_source_pairs = set(
        (
            (high_metrics.get("selection_population") or {}).get("source_pair_counts")
            or {}
        ).keys()
    )
    if not validated_source_pairs:
        frappe.throw("The approved High validation has no represented source pairs")
    return {
        "policy_doc": policy_doc,
        "policy": policy,
        "snapshot": snapshot,
        "snapshot_sha256": snapshot_sha256,
        "high_run": high_run,
        "threshold_run": threshold_run,
        "review_threshold": float(review_threshold),
        "validated_source_pairs": validated_source_pairs,
    }


def _promote_policy_to_pilot(policy_name: str) -> dict[str, Any]:
    prerequisites = _canary_prerequisites(policy_name)
    policy_doc = prerequisites["policy_doc"]
    if policy_doc.status == "Pilot":
        return {"policy": policy_doc.name, "status": "Pilot", "changed": False}
    if policy_doc.status != "Draft":
        frappe.throw("Only the unchanged Draft policy can be promoted to Pilot")
    policy_doc.status = "Pilot"
    policy_doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"policy": policy_doc.name, "status": "Pilot", "changed": True}


@frappe.whitelist()
def promote_policy_to_pilot(
    policy_name: str = DEFAULT_PILOT_POLICY_VERSION,
) -> dict[str, Any]:
    _require_manager()
    return _promote_policy_to_pilot(policy_name)


def install_promote_policy_to_pilot(
    policy_name: str = DEFAULT_PILOT_POLICY_VERSION,
) -> dict[str, Any]:
    """Bench-only policy promotion after both required approvals."""
    return _promote_policy_to_pilot(policy_name)


def _create_canary_run(policy_name: str) -> dict[str, str]:
    prerequisites = _canary_prerequisites(policy_name)
    policy_doc = prerequisites["policy_doc"]
    if policy_doc.status != "Pilot":
        frappe.throw("The matching policy must be in Pilot status")
    existing = frappe.get_all(
        RUN_DOCTYPE,
        filters={"matching_policy": policy_name, "status": ["in", list(RUNNING_STATUSES)]},
        pluck="name",
        limit=1,
    )
    if existing:
        frappe.throw(f"Canary run {existing[0]} is already in progress")
    run = frappe.get_doc(
        {
            "doctype": RUN_DOCTYPE,
            "matching_policy": policy_name,
            "policy_version": prerequisites["policy"].version,
            "policy_snapshot_json": _json(prerequisites["snapshot"]),
            "policy_snapshot_sha256": prerequisites["snapshot_sha256"],
            "high_validation_run": prerequisites["high_run"].name,
            "threshold_evaluation_run": prerequisites["threshold_run"].name,
            "splink_review_threshold": prerequisites["review_threshold"],
            "status": "Queued",
            "snapshot_at": frappe.utils.now_datetime(),
        }
    ).insert(ignore_permissions=True)
    frappe.enqueue(
        "db_connector.api_fuzzy_canary.run_canary",
        queue="long",
        timeout=14_400,
        enqueue_after_commit=True,
        run_name=run.name,
    )
    frappe.db.commit()
    return {"run": run.name, "status": "Queued"}


@frappe.whitelist()
def enqueue_canary(
    policy_name: str = DEFAULT_PILOT_POLICY_VERSION,
) -> dict[str, str]:
    _require_manager()
    return _create_canary_run(policy_name)


def install_canary_run(
    policy_name: str = DEFAULT_PILOT_POLICY_VERSION,
) -> dict[str, str]:
    """Bench-only canary launcher; no credential is required in shell history."""
    return _create_canary_run(policy_name)


def _set_run_status(run: Any, status: str) -> None:
    run.db_set("status", status, update_modified=False)
    frappe.db.commit()


def _trusted_id_metadata(record: dict[str, Any], policy: MatchingPolicy) -> dict[str, str]:
    source = str(record.get("source") or "")
    output = {}
    for attribute in policy.trusted_global_identifiers:
        if not policy.globally_comparable(source, attribute):
            continue
        raw_value = record.get(attribute)
        if attribute == "hkid" and not norm.valid_hkid(raw_value):
            continue
        value = norm.identifier(raw_value)
        if value:
            output[attribute] = value
    return output


def _stale_record_ids(
    record_by_id: dict[str, dict[str, Any]],
    record_ids: set[str],
) -> set[str]:
    current = {}
    ordered = sorted(record_ids)
    for index in range(0, len(ordered), 500):
        for row in frappe.get_all(
            "CCD Master",
            filters={"name": ["in", ordered[index : index + 500]]},
            fields=["name", "modified"],
            limit_page_length=500,
        ):
            current[str(row.name)] = str(row.modified)
    return {
        record_id
        for record_id in record_ids
        if current.get(record_id) != str(record_by_id[record_id].get("source_modified"))
    }


def _append_event(
    recommendation: Any,
    event_type: str,
    from_status: str,
    to_status: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    frappe.get_doc(
        {
            "doctype": EVENT_DOCTYPE,
            "recommendation": recommendation.name,
            "canary_run": recommendation.canary_run,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "event_at": frappe.utils.now_datetime(),
            "actor": frappe.session.user or "Administrator",
            "metadata_json": _json(metadata or {}),
        }
    ).insert(ignore_permissions=True)


def _refresh_run_counts(run_name: str) -> dict[str, int]:
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name},
        fields=["status", "count(name) as count"],
        group_by="status",
    )
    counts = {str(row.status): int(row.count) for row in rows}
    values = {
        "proposed_count": counts.get("Proposed", 0),
        "exception_count": counts.get("Exception", 0),
        "active_count": counts.get("Active", 0),
        "reversed_count": counts.get("Reversed", 0),
        "superseded_count": counts.get("Superseded", 0),
    }
    frappe.db.set_value(RUN_DOCTYPE, run_name, values, update_modified=False)
    return values


def run_canary(run_name: str) -> None:
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Queued":
        frappe.throw("Only a queued canary run may execute")
    _set_run_status(run, "Profiling")
    try:
        policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
        sources = policy.sources()
        placeholders = ", ".join(["%s"] * len(sources))
        raw_rows = frappe.db.sql(
            f"""SELECT * FROM `tabCCD Master`
                WHERE modified <= %s AND ccd_reg_source IN ({placeholders})""",
            (run.snapshot_at, *sources),
            as_dict=True,
        )
        records = [_canonical_record(dict(row), policy) for row in raw_rows]
        record_by_id = {
            str(row["record_id"]): row for row in records if row.get("record_id")
        }
        run.db_set("record_count", len(records), update_modified=False)

        _set_run_status(run, "Generating Candidates")
        blocked = generate_candidate_pairs(records, policy)
        run.db_set("candidate_count", len(blocked.pairs), update_modified=False)
        run.db_set("candidate_truncated", int(blocked.truncated), update_modified=False)
        run.db_set("skipped_blocks_json", _json(blocked.skipped_blocks), update_modified=False)
        if blocked.truncated or blocked.skipped_blocks:
            frappe.throw(
                "Canary safety requires complete candidate generation; truncation or skipped blocks were found"
            )

        _set_run_status(run, "Applying Safety Gates")
        high_edges: list[CanaryEdge] = []
        conflicting_pairs: set[tuple[str, str]] = set()
        for pair in blocked.pairs:
            left = record_by_id[pair.left_id]
            right = record_by_id[pair.right_id]
            evidence = build_evidence(left, right, policy)
            trusted = frozenset(
                attribute
                for attribute in policy.trusted_global_identifiers
                if policy.globally_comparable(left["source"], attribute)
                and policy.globally_comparable(right["source"], attribute)
            )
            result = tiered_result(
                evidence,
                policy,
                conflict_mode="gated",
                trusted_identifiers=trusted,
            )
            if result.tier == MatchTier.HIGH:
                high_edges.append(
                    CanaryEdge(
                        pair.left_id,
                        pair.right_id,
                        left["source"],
                        right["source"],
                        pair.source_pair,
                        tuple(pair.blocking_routes),
                        tuple(result.reasons),
                        APPROVED_HIGH_REASON in result.reasons,
                    )
                )
            elif result.tier == MatchTier.CONFLICT:
                conflicting_pairs.add(ordered_pair(pair.left_id, pair.right_id))

        high_record_ids = {
            item
            for edge in high_edges
            for item in (edge.left_id, edge.right_id)
        }
        gate_records = {
            record_id: {
                "source": record_by_id[record_id]["source"],
                "trusted_ids": _trusted_id_metadata(record_by_id[record_id], policy),
            }
            for record_id in high_record_ids
        }
        stale = _stale_record_ids(record_by_id, high_record_ids)
        high_metrics = json.loads(
            frappe.db.get_value(
                "CCD Match Evaluation Run", run.high_validation_run, "metrics_json"
            )
            or "{}"
        ).get("high_tier_validation") or {}
        validated_source_pairs = set(
            (
                (high_metrics.get("selection_population") or {}).get("source_pair_counts")
                or {}
            ).keys()
        )
        decisions = analyze_canary_edges(
            high_edges,
            gate_records,
            validated_source_pairs=validated_source_pairs,
            conflicting_pairs=conflicting_pairs,
            stale_record_ids=stale,
        )

        _set_run_status(run, "Writing Recommendations")
        reason_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        cluster_fingerprints = set()
        largest_cluster = 0
        for edge in high_edges:
            decision = decisions[edge.pair_key]
            reason_counts.update(decision.reasons)
            status_counts[decision.status] += 1
            cluster_fingerprints.add(decision.cluster_fingerprint)
            largest_cluster = max(largest_cluster, decision.cluster_size)
            left_id, right_id = edge.pair_key
            left = record_by_id[left_id]
            right = record_by_id[right_id]
            recommendation = frappe.get_doc(
                {
                    "doctype": RECOMMENDATION_DOCTYPE,
                    "canary_run": run.name,
                    "matching_policy": run.matching_policy,
                    "policy_version": run.policy_version,
                    "recommendation_key": _recommendation_key(run.name, left_id, right_id),
                    "pair_fingerprint": _pair_fingerprint(run.policy_version, left_id, right_id),
                    "left_record": left_id,
                    "right_record": right_id,
                    "left_source": left["source"],
                    "right_source": right["source"],
                    "source_pair": edge.source_pair,
                    "left_modified_at": left["source_modified"],
                    "right_modified_at": right["source_modified"],
                    "model_tier": "High",
                    "blocking_routes": ", ".join(edge.blocking_routes),
                    "reason_codes_json": _json(edge.reason_codes),
                    "cluster_fingerprint": decision.cluster_fingerprint,
                    "cluster_size": decision.cluster_size,
                    "status": decision.status,
                    "safety_reasons_json": _json(decision.reasons),
                }
            ).insert(ignore_permissions=True)
            _append_event(
                recommendation,
                "Created" if decision.status == "Proposed" else "Safety Exception",
                "",
                decision.status,
                (
                    "passed_all_canary_safety_gates"
                    if decision.status == "Proposed"
                    else ",".join(decision.reasons)
                ),
            )

        summary = {
            "policy_snapshot_sha256": run.policy_snapshot_sha256,
            "blocking_version": BLOCKING_VERSION,
            "approved_high_rule": APPROVED_HIGH_REASON,
            "approved_high_validation_run": run.high_validation_run,
            "approved_threshold_evaluation_run": run.threshold_evaluation_run,
            "splink_review_threshold": float(run.splink_review_threshold or 0),
            "validated_source_pair_count": len(validated_source_pairs),
            "high_candidate_count": len(high_edges),
            "status_counts": dict(sorted(status_counts.items())),
            "safety_reason_counts": dict(sorted(reason_counts.items())),
            "high_component_count": len(cluster_fingerprints),
            "largest_component_size": largest_cluster,
            "model_conflict_candidate_count": len(conflicting_pairs),
            "stale_record_count": len(stale),
            "production_records_modified": False,
        }
        run.db_set("high_candidate_count", len(high_edges), update_modified=False)
        run.db_set("summary_json", _json(summary), update_modified=False)
        _refresh_run_counts(run.name)
        run.db_set("status", "Ready", update_modified=False)
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            RUN_DOCTYPE,
            run_name,
            {
                "status": "Failed",
                "error_summary": f"canary_generation_failed:{type(exc).__name__}",
            },
            update_modified=False,
        )
        frappe.log_error(traceback.format_exc(), "CCD recommendation canary failed")
        frappe.db.commit()
        raise


def _change_recommendation_status(
    recommendation: Any,
    to_status: str,
    event_type: str,
    reason: str,
    *,
    activated: bool = False,
) -> None:
    from_status = str(recommendation.status)
    values: dict[str, Any] = {"status": to_status}
    now = frappe.utils.now_datetime()
    actor = frappe.session.user or "Administrator"
    if activated:
        values.update({"activated_at": now, "activated_by": actor})
    if to_status in {"Reversed", "Superseded"}:
        values.update({"ended_at": now, "ended_by": actor, "end_reason": reason})
    frappe.db.set_value(
        RECOMMENDATION_DOCTYPE,
        recommendation.name,
        values,
        update_modified=False,
    )
    recommendation.status = to_status
    _append_event(recommendation, event_type, from_status, to_status, reason)


def _stale_recommendation_names(run_name: str) -> list[str]:
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name, "status": "Proposed"},
        fields=["name", "left_record", "right_record", "left_modified_at", "right_modified_at"],
        limit_page_length=100_000,
    )
    record_ids = sorted(
        {
            str(item)
            for row in rows
            for item in (row.left_record, row.right_record)
        }
    )
    current = {}
    for index in range(0, len(record_ids), 500):
        for row in frappe.get_all(
            "CCD Master",
            filters={"name": ["in", record_ids[index : index + 500]]},
            fields=["name", "modified"],
            limit_page_length=500,
        ):
            current[str(row.name)] = str(row.modified)
    return [
        row.name
        for row in rows
        if current.get(str(row.left_record)) != str(row.left_modified_at)
        or current.get(str(row.right_record)) != str(row.right_modified_at)
    ]


@frappe.whitelist()
def activate_canary(run_name: str) -> dict[str, Any]:
    _require_manager()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Ready":
        frappe.throw("Only a Ready canary may be activated")
    policy = frappe.get_doc("CCD Matching Policy", run.matching_policy)
    if policy.status != "Pilot":
        frappe.throw("The matching policy is no longer in Pilot status")
    if _snapshot_hash(_policy_snapshot(_policy_from_doc(policy))) != run.policy_snapshot_sha256:
        frappe.throw("The matching policy changed after this canary snapshot")

    stale_names = _stale_recommendation_names(run.name)
    if stale_names:
        for name in stale_names:
            recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, name)
            _change_recommendation_status(
                recommendation,
                "Exception",
                "Safety Exception",
                "stale_before_activation",
            )
        _refresh_run_counts(run.name)
        frappe.db.commit()
        return {
            "run": run.name,
            "status": "Ready",
            "activated": 0,
            "new_stale_exceptions": len(stale_names),
        }

    proposed = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run.name, "status": "Proposed"},
        fields=["name", "status", "canary_run", "pair_fingerprint"],
        limit_page_length=100_000,
    )
    prior = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={
            "matching_policy": run.matching_policy,
            "status": "Active",
            "canary_run": ["!=", run.name],
        },
        fields=["name", "status", "canary_run", "pair_fingerprint"],
        limit_page_length=100_000,
    )
    prior_by_pair = {row.pair_fingerprint: row.name for row in prior}
    for old in prior:
        _change_recommendation_status(
            old,
            "Superseded",
            "Superseded",
            f"superseded_by_canary:{run.name}",
        )
    for recommendation in proposed:
        old_name = prior_by_pair.get(recommendation.pair_fingerprint)
        if old_name:
            frappe.db.set_value(
                RECOMMENDATION_DOCTYPE,
                recommendation.name,
                "supersedes",
                old_name,
                update_modified=False,
            )
        _change_recommendation_status(
            recommendation,
            "Active",
            "Activated",
            "canary_activation",
            activated=True,
        )
    for old_run in set(row.canary_run for row in prior):
        frappe.db.set_value(RUN_DOCTYPE, old_run, "status", "Completed", update_modified=False)
        _refresh_run_counts(old_run)
    counts = _refresh_run_counts(run.name)
    frappe.db.set_value(
        RUN_DOCTYPE,
        run.name,
        {
            "status": "Active",
            "activated_at": frappe.utils.now_datetime(),
            "activated_by": frappe.session.user,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {"run": run.name, "status": "Active", "activated": counts["active_count"]}


@frappe.whitelist()
def reverse_recommendation(recommendation_name: str, reason: str) -> dict[str, str]:
    _require_manager()
    if not str(reason or "").strip():
        frappe.throw("A reversal reason is required")
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if recommendation.status not in {"Proposed", "Active"}:
        frappe.throw("Only a Proposed or Active recommendation may be reversed")
    _change_recommendation_status(
        recommendation,
        "Reversed",
        "Reversed",
        str(reason).strip(),
    )
    _refresh_run_counts(recommendation.canary_run)
    frappe.db.commit()
    return {"recommendation": recommendation.name, "status": "Reversed"}


@frappe.whitelist()
def reverse_canary(run_name: str, reason: str) -> dict[str, Any]:
    _require_manager()
    if not str(reason or "").strip():
        frappe.throw("A reversal reason is required")
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Active":
        frappe.throw("Only an Active canary may be reversed")
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run.name, "status": "Active"},
        fields=["name", "status", "canary_run"],
        limit_page_length=100_000,
    )
    for recommendation in rows:
        _change_recommendation_status(
            recommendation,
            "Reversed",
            "Reversed",
            str(reason).strip(),
        )
    counts = _refresh_run_counts(run.name)
    run.db_set("status", "Completed", update_modified=False)
    frappe.db.commit()
    return {"run": run.name, "status": "Completed", "reversed": counts["reversed_count"]}


@frappe.whitelist()
def get_canary_summary(run_name: str) -> dict[str, Any]:
    _require_manager()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    return {
        "run": run.name,
        "status": run.status,
        "policy_version": run.policy_version,
        "snapshot_at": run.snapshot_at,
        "record_count": run.record_count,
        "candidate_count": run.candidate_count,
        "candidate_truncated": bool(run.candidate_truncated),
        "skipped_block_count": len(json.loads(run.skipped_blocks_json or "[]")),
        "high_candidate_count": run.high_candidate_count,
        "proposed_count": run.proposed_count,
        "exception_count": run.exception_count,
        "active_count": run.active_count,
        "reversed_count": run.reversed_count,
        "superseded_count": run.superseded_count,
        "summary": json.loads(run.summary_json or "{}"),
    }
