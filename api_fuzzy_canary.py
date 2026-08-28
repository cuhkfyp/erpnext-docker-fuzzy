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
    REVIEW_ROLE,
    SENSITIVE_ROLE,
    THRESHOLD_EVALUATION,
    _canonical_record,
    _policy_from_doc,
    _policy_snapshot,
)
from db_connector.fuzzy_matching import normalization as norm
from db_connector.fuzzy_matching.blocking import BLOCKING_VERSION, generate_candidate_pairs
from db_connector.fuzzy_matching.canary import (
    CanaryEdge,
    analyze_canary_edges,
    canonical_identity_groups,
    identity_partition_fingerprint,
    ordered_pair,
)
from db_connector.fuzzy_matching.correction import partition_for_display
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.models import build_evidence, tiered_result
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.security import mask_identifier
from db_connector.fuzzy_matching.types import MatchTier

RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
EVENT_DOCTYPE = "CCD Match Recommendation Event"
COMPONENT_REVIEW_DOCTYPE = "CCD Match Component Review"
APPROVED_HIGH_REASON = "exact_name_plus_independent_evidence"
QC_SAMPLE_SIZE = 100
COMPONENT_DECISIONS = {"All Same", "Partial Match", "All Different", "Unsure"}
FINAL_COMPONENT_DECISIONS = COMPONENT_DECISIONS - {"Unsure"}
FINAL_REVIEW_STATUSES = {"Agreed", "Adjudicated"}
OPEN_REVIEW_STATUSES = {
    "Unreviewed",
    "Partially Reviewed",
    "Positive Confirmation Required",
}
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


def _require_reviewer() -> None:
    roles = set(frappe.get_roles())
    if "System Manager" not in roles and REVIEW_ROLE not in roles and SENSITIVE_ROLE not in roles:
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _has_sensitive_access() -> bool:
    roles = set(frappe.get_roles())
    return "System Manager" in roles or SENSITIVE_ROLE in roles


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
        "active_count": counts.get("Approved", 0) + counts.get("Active", 0),
        "reversed_count": counts.get("Withdrawn", 0) + counts.get("Reversed", 0),
        "superseded_count": counts.get("Superseded", 0),
    }
    frappe.db.set_value(RUN_DOCTYPE, run_name, values, update_modified=False)
    return values


def _component_review_key(run_name: str, cluster_fingerprint: str) -> str:
    return hashlib.sha256(f"{run_name}\x1f{cluster_fingerprint}".encode()).hexdigest()


def _refresh_review_workflow_counts(run_name: str) -> dict[str, int]:
    component_count = frappe.db.count(COMPONENT_REVIEW_DOCTYPE, {"canary_run": run_name})
    component_complete = frappe.db.count(
        COMPONENT_REVIEW_DOCTYPE,
        {"canary_run": run_name, "review_status": ["in", sorted(FINAL_REVIEW_STATUSES)]},
    )
    qc_count = frappe.db.count(
        RECOMMENDATION_DOCTYPE,
        {"canary_run": run_name, "qc_selected": 1},
    )
    qc_complete = frappe.db.count(
        RECOMMENDATION_DOCTYPE,
        {
            "canary_run": run_name,
            "qc_selected": 1,
            "qc_review_status": ["in", sorted(FINAL_REVIEW_STATUSES)],
        },
    )
    values = {
        "exception_component_count": component_count,
        "exception_review_complete_count": component_complete,
        "qc_sample_count": qc_count,
        "qc_review_complete_count": qc_complete,
    }
    frappe.db.set_value(RUN_DOCTYPE, run_name, values, update_modified=False)
    return values


def _initialize_review_workflow(run_name: str) -> dict[str, int]:
    """Create one human-review case per exception component and a stable QC sample."""
    exception_rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name, "status": "Exception"},
        fields=["cluster_fingerprint", "cluster_size", "count(name) as recommendation_count"],
        group_by="cluster_fingerprint, cluster_size",
        limit_page_length=100_000,
    )
    existing = {
        str(row.review_key): row.name
        for row in frappe.get_all(
            COMPONENT_REVIEW_DOCTYPE,
            filters={"canary_run": run_name},
            fields=["name", "review_key"],
            limit_page_length=100_000,
        )
    }
    for row in exception_rows:
        fingerprint = str(row.cluster_fingerprint)
        review_key = _component_review_key(run_name, fingerprint)
        review_name = existing.get(review_key)
        if not review_name:
            review = frappe.get_doc(
                {
                    "doctype": COMPONENT_REVIEW_DOCTYPE,
                    "canary_run": run_name,
                    "review_key": review_key,
                    "cluster_fingerprint": fingerprint,
                    "cluster_size": int(row.cluster_size or 0),
                    "recommendation_count": int(row.recommendation_count or 0),
                    "review_status": "Unreviewed",
                }
            ).insert(ignore_permissions=True)
            review_name = review.name
        frappe.db.sql(
            f"""UPDATE `tab{RECOMMENDATION_DOCTYPE}`
                SET component_review = %s
                WHERE canary_run = %s AND cluster_fingerprint = %s
                  AND status = 'Exception'""",
            (review_name, run_name, fingerprint),
        )

    already_selected = frappe.db.count(
        RECOMMENDATION_DOCTYPE,
        {"canary_run": run_name, "qc_selected": 1},
    )
    if not already_selected:
        proposed = frappe.get_all(
            RECOMMENDATION_DOCTYPE,
            filters={"canary_run": run_name, "status": "Proposed"},
            fields=["name", "recommendation_key"],
            limit_page_length=100_000,
        )
        selected = sorted(
            proposed,
            key=lambda row: hashlib.sha256(
                f"{run_name}\x1f{row.recommendation_key}".encode()
            ).hexdigest(),
        )[: min(QC_SAMPLE_SIZE, len(proposed))]
        for row in selected:
            frappe.db.set_value(
                RECOMMENDATION_DOCTYPE,
                row.name,
                {"qc_selected": 1, "qc_review_status": "Unreviewed"},
                update_modified=False,
            )
    return _refresh_review_workflow_counts(run_name)


def install_canary_review_workflow(run_name: str) -> dict[str, Any]:
    """Bench-only idempotent backfill for a canary created before the review UI."""
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status not in {"Ready", "Active", "Completed"}:
        frappe.throw("The canary must have finished generating recommendations")
    counts = _initialize_review_workflow(run.name)
    frappe.db.commit()
    return {"run": run.name, **counts}


def install_existing_canary_review_workflows() -> dict[str, Any]:
    """Idempotently add review cases to all completed recommendation generations."""
    output = []
    for run_name in frappe.get_all(
        RUN_DOCTYPE,
        filters={"status": ["in", ["Ready", "Active", "Completed"]]},
        order_by="creation",
        pluck="name",
        limit_page_length=10_000,
    ):
        if not frappe.db.count(RECOMMENDATION_DOCTYPE, {"canary_run": run_name}):
            continue
        output.append({"run": run_name, **_initialize_review_workflow(run_name)})
    frappe.db.commit()
    return {"runs": output, "run_count": len(output)}


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
                    "left_identity_fingerprint": identity_fingerprint(left, policy),
                    "right_identity_fingerprint": identity_fingerprint(right, policy),
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

        review_workflow = _initialize_review_workflow(run.name)
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
            "exception_component_count": review_workflow["exception_component_count"],
            "random_qc_sample_count": review_workflow["qc_sample_count"],
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


def _masked_evidence_value(value: Any) -> str:
    """Mask every identity value for ordinary reviewers, preserving equality clues."""
    return mask_identifier(value, visible_suffix=2)


def _display_evidence_value(value: Any, sensitive: bool) -> str:
    raw = str(value or "").strip()
    return raw if sensitive else _masked_evidence_value(raw)


def _run_and_policy(run_name: str) -> tuple[Any, MatchingPolicy]:
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    return run, MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))


def _recommendation_stale(recommendation: Any) -> bool:
    left_modified = frappe.db.get_value("CCD Master", recommendation.left_record, "modified")
    right_modified = frappe.db.get_value("CCD Master", recommendation.right_record, "modified")
    return (
        str(left_modified or "") != str(recommendation.left_modified_at or "")
        or str(right_modified or "") != str(recommendation.right_modified_at or "")
    )


def _pair_evidence_payload(recommendation: Any) -> dict[str, Any]:
    _run, policy = _run_and_policy(recommendation.canary_run)
    left = frappe.get_doc("CCD Master", recommendation.left_record).as_dict()
    right = frappe.get_doc("CCD Master", recommendation.right_record).as_dict()
    left["source"] = recommendation.left_source
    right["source"] = recommendation.right_source
    evidence = build_evidence(left, right, policy)
    sensitive = _has_sensitive_access()
    attributes = []
    for attribute in policy.attributes():
        item = evidence.get(attribute)
        attributes.append(
            {
                "attribute": attribute,
                "left": _display_evidence_value(policy.value(left, attribute), sensitive),
                "right": _display_evidence_value(policy.value(right, attribute), sensitive),
                "comparison": str(item.level.value if item else "not_compared"),
            }
        )
    payload = {
        "recommendation": recommendation.name,
        "status": recommendation.status,
        "left": {"alias": "Left", "source": recommendation.left_source},
        "right": {"alias": "Right", "source": recommendation.right_source},
        "attributes": attributes,
        "sensitive_values_visible": sensitive,
        "stale": _recommendation_stale(recommendation),
        "component_review": recommendation.component_review or "",
        "qc_selected": bool(recommendation.qc_selected),
        "qc_review_status": recommendation.qc_review_status or "",
        "qc_final_label": recommendation.qc_final_label or "",
        "qc_assigned_at": recommendation.qc_assigned_at or "",
        "qc_due_at": recommendation.qc_due_at or "",
        "qc_failure_action": recommendation.qc_failure_action or "",
    }
    if sensitive:
        payload["left"]["record_id"] = recommendation.left_record
        payload["right"]["record_id"] = recommendation.right_record
    if "System Manager" in set(frappe.get_roles()):
        payload["reason_codes"] = json.loads(recommendation.reason_codes_json or "[]")
        payload["safety_reasons"] = json.loads(
            recommendation.safety_reasons_json or "[]"
        )
    return payload


@frappe.whitelist()
def get_recommendation_evidence(recommendation_name: str) -> dict[str, Any]:
    _require_reviewer()
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    payload = _pair_evidence_payload(recommendation)
    ordinary = [
        row
        for row in recommendation.get("qc_review_labels") or []
        if not row.is_adjudication
    ]
    submitted = any(row.reviewer == frappe.session.user for row in ordinary)
    payload["can_submit_qc"] = bool(
        recommendation.qc_selected
        and recommendation.qc_assigned_at
        and not payload["stale"]
        and recommendation.qc_review_status in OPEN_REVIEW_STATUSES
        and not submitted
    )
    payload["can_adjudicate_qc"] = bool(
        "System Manager" in set(frappe.get_roles())
        and not payload["stale"]
        and recommendation.qc_review_status == "Needs Adjudication"
    )
    return payload


def _component_context(review: Any) -> tuple[list[Any], list[str], dict[str, Any]]:
    recommendations = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={
            "canary_run": review.canary_run,
            "cluster_fingerprint": review.cluster_fingerprint,
        },
        fields=[
            "name",
            "left_record",
            "right_record",
            "left_source",
            "right_source",
            "left_modified_at",
            "right_modified_at",
        ],
        order_by="creation",
        limit_page_length=100_000,
    )
    record_ids = sorted(
        {
            str(item)
            for row in recommendations
            for item in (row.left_record, row.right_record)
        }
    )
    expected_modified: dict[str, str] = {}
    for row in recommendations:
        expected_modified[str(row.left_record)] = str(row.left_modified_at or "")
        expected_modified[str(row.right_record)] = str(row.right_modified_at or "")
    return recommendations, record_ids, expected_modified


def _component_stale(review: Any) -> bool:
    _rows, record_ids, expected = _component_context(review)
    current = {
        str(row.name): str(row.modified or "")
        for row in frappe.get_all(
            "CCD Master",
            filters={"name": ["in", record_ids]},
            fields=["name", "modified"],
            limit_page_length=100,
        )
    }
    return any(current.get(record_id, "") != expected.get(record_id, "") for record_id in record_ids)


def _component_aliases(record_ids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    by_record = {record_id: f"R{index + 1}" for index, record_id in enumerate(record_ids)}
    return by_record, {alias: record_id for record_id, alias in by_record.items()}


def _groups_for_component_decision(
    decision: str,
    record_ids: list[str],
    aliases_to_records: dict[str, str],
    same_pairs_json: str | list[Any] | None,
) -> tuple[tuple[str, ...], ...]:
    if decision not in COMPONENT_DECISIONS:
        frappe.throw("Decision must be All Same, Partial Match, All Different, or Unsure")
    if decision == "Unsure":
        return tuple()
    if decision == "All Same":
        return (tuple(sorted(record_ids)),)
    if decision == "All Different":
        return tuple((record_id,) for record_id in sorted(record_ids))
    raw_pairs = (
        json.loads(same_pairs_json or "[]")
        if isinstance(same_pairs_json, str)
        else (same_pairs_json or [])
    )
    same_pairs = []
    for raw_pair in raw_pairs:
        values = str(raw_pair).split("|")
        if len(values) != 2 or any(value not in aliases_to_records for value in values):
            frappe.throw("A Partial Match selection contains an invalid record pair")
        same_pairs.append(
            (aliases_to_records[values[0]], aliases_to_records[values[1]])
        )
    if not same_pairs:
        frappe.throw("Partial Match requires at least one Same pair")
    try:
        groups = canonical_identity_groups(record_ids, same_pairs)
    except ValueError as exc:
        frappe.throw(str(exc))
    if len(groups) in {1, len(record_ids)}:
        frappe.throw("Use All Same or All Different for this selection")
    return groups


def _component_decision_fingerprint(
    decision: str, groups: tuple[tuple[str, ...], ...]
) -> str:
    if decision == "Unsure":
        return hashlib.sha256(b"Unsure").hexdigest()
    return hashlib.sha256(
        f"{decision}\x1f{identity_partition_fingerprint(groups)}".encode()
    ).hexdigest()


def _finalize_component_review(
    review: Any,
    decision: str,
    groups_json: str,
    status: str,
) -> None:
    review.review_status = status
    review.final_decision = decision
    review.final_groups_json = groups_json
    review.finalized_at = frappe.utils.now_datetime()
    review.finalized_by = frappe.session.user


def _append_component_submission(
    review: Any,
    decision: str,
    groups: tuple[tuple[str, ...], ...],
    notes: str,
    *,
    is_adjudication: bool,
) -> Any:
    groups_json = _json(groups)
    return review.append(
        "review_submissions",
        {
            "reviewer": frappe.session.user,
            "decision": decision,
            "decision_fingerprint": _component_decision_fingerprint(decision, groups),
            "groups_json": groups_json,
            "notes": str(notes or "").strip(),
            "submitted_at": frappe.utils.now_datetime(),
            "is_adjudication": int(is_adjudication),
        },
    )


def _current_component_identity_result(
    review: Any,
    aliases: dict[str, str],
    *,
    reveal_record_ids: bool,
) -> dict[str, Any]:
    """Return the latest decision in the original decision's supersession chain."""
    decision_name = str(review.identity_decision or "").strip()
    if not decision_name:
        return {}

    seen: set[str] = set()
    decision = None
    while decision_name:
        if decision_name in seen:
            frappe.log_error(
                title="CCD identity decision supersession cycle",
                message=f"Component review {review.name}: {decision_name}",
            )
            break
        seen.add(decision_name)
        row = frappe.db.get_value(
            "CCD Identity Decision",
            decision_name,
            [
                "name",
                "decision_version",
                "decision_type",
                "origin",
                "origin_doctype",
                "origin_document",
                "final_groups_json",
                "status",
                "superseded_by",
            ],
            as_dict=True,
        )
        if not row:
            break
        decision = row
        decision_name = str(row.superseded_by or "").strip()

    if not decision:
        return {}

    try:
        raw_groups = json.loads(decision.final_groups_json or "[]")
    except (TypeError, ValueError):
        raw_groups = []
    groups, outside_count = partition_for_display(
        (group for group in raw_groups if isinstance(group, list))
        if isinstance(raw_groups, list)
        else (),
        aliases,
        reveal_record_ids=reveal_record_ids,
    )

    return {
        "identity_decision": str(decision.name),
        "decision_version": int(decision.decision_version or 1),
        "decision_type": str(decision.decision_type or ""),
        "origin": str(decision.origin or ""),
        "status": str(decision.status or ""),
        "groups": groups,
        "outside_component_record_count": outside_count,
        "correction": (
            str(decision.origin_document or "")
            if str(decision.origin_doctype or "") == "CCD Identity Correction"
            else ""
        ),
    }


@frappe.whitelist()
def get_component_evidence(review_name: str) -> dict[str, Any]:
    _require_reviewer()
    review = frappe.get_doc(COMPONENT_REVIEW_DOCTYPE, review_name)
    recommendations, record_ids, _expected = _component_context(review)
    aliases, _reverse = _component_aliases(record_ids)
    _run, policy = _run_and_policy(review.canary_run)
    raw_rows = frappe.get_all(
        "CCD Master",
        filters={"name": ["in", record_ids]},
        fields=["*"],
        limit_page_length=100,
    )
    raw_by_id = {str(row.name): dict(row) for row in raw_rows}
    source_by_id: dict[str, str] = {}
    for recommendation in recommendations:
        source_by_id[str(recommendation.left_record)] = recommendation.left_source
        source_by_id[str(recommendation.right_record)] = recommendation.right_source
    sensitive = _has_sensitive_access()
    records = []
    for record_id in record_ids:
        raw = raw_by_id[record_id]
        records.append(
            {
                "alias": aliases[record_id],
                "source": source_by_id.get(record_id, ""),
                "attributes": {
                    attribute: _display_evidence_value(
                        policy.value(raw, attribute), sensitive
                    )
                    for attribute in policy.attributes()
                },
                **({"record_id": record_id} if sensitive else {}),
            }
        )
    candidate_pairs = [
        {
            "left": aliases[str(row.left_record)],
            "right": aliases[str(row.right_record)],
        }
        for row in recommendations
    ]
    pair_options = [
        {
            "value": f"{aliases[left]}|{aliases[right]}",
            "label": (
                f"{aliases[left]} ({source_by_id.get(left, '')}) = "
                f"{aliases[right]} ({source_by_id.get(right, '')})"
            ),
        }
        for index, left in enumerate(record_ids)
        for right in record_ids[index + 1 :]
    ]
    stale = _component_stale(review)
    if stale and not review.stale:
        frappe.db.set_value(
            COMPONENT_REVIEW_DOCTYPE,
            review.name,
            {"stale": 1, "review_status": "Stale"},
            update_modified=False,
        )
    ordinary = [row for row in review.review_submissions if not row.is_adjudication]
    submitted = any(row.reviewer == frappe.session.user for row in ordinary)
    final_groups = []
    if review.final_groups_json:
        for group in json.loads(review.final_groups_json):
            final_groups.append([aliases[str(record_id)] for record_id in group])
    current_identity_result = _current_component_identity_result(
        review,
        aliases,
        reveal_record_ids=sensitive,
    )
    return {
        "review": review.name,
        "status": "Stale" if stale else review.review_status,
        "final_decision": review.final_decision or "",
        "final_groups": final_groups,
        "materialization_status": review.materialization_status or "Not Final",
        "identity_decision": review.identity_decision or "",
        "correction_decision": review.correction_decision or "",
        "current_identity_result": current_identity_result,
        "materialization_error": review.materialization_error or "",
        "records": records,
        "attributes": list(policy.attributes()),
        "candidate_pairs": candidate_pairs,
        "pair_options": pair_options,
        "sensitive_values_visible": sensitive,
        "stale": stale,
        "can_submit": bool(
            not stale
            and review.review_status in OPEN_REVIEW_STATUSES
            and not submitted
        ),
        "can_adjudicate": bool(
            "System Manager" in set(frappe.get_roles())
            and not stale
            and review.review_status == "Needs Adjudication"
        ),
        "can_materialize": bool(
            "System Manager" in set(frappe.get_roles())
            and review.review_status in FINAL_REVIEW_STATUSES
            and review.materialization_status in {"Pending", "Exception"}
        ),
    }


@frappe.whitelist()
def submit_component_review(
    review_name: str,
    decision: str,
    same_pairs_json: str = "[]",
    notes: str = "",
) -> dict[str, str]:
    _require_reviewer()
    review = frappe.get_doc(COMPONENT_REVIEW_DOCTYPE, review_name)
    if _component_stale(review):
        frappe.db.set_value(
            COMPONENT_REVIEW_DOCTYPE,
            review.name,
            {"stale": 1, "review_status": "Stale"},
            update_modified=False,
        )
        frappe.throw("This component is stale. Create a new canary before reviewing it.")
    if review.review_status not in OPEN_REVIEW_STATUSES:
        frappe.throw("This component is closed to ordinary review")
    ordinary = [row for row in review.review_submissions if not row.is_adjudication]
    if any(row.reviewer == frappe.session.user for row in ordinary):
        frappe.throw("Your immutable component review is already recorded")
    _recommendations, record_ids, _expected = _component_context(review)
    _aliases, aliases_to_records = _component_aliases(record_ids)
    groups = _groups_for_component_decision(
        decision, record_ids, aliases_to_records, same_pairs_json
    )
    submission = _append_component_submission(
        review, decision, groups, notes, is_adjudication=False
    )
    adjudications = [row for row in review.review_submissions if row.is_adjudication]
    if adjudications:
        adjudication = adjudications[-1]
        if submission.decision_fingerprint == adjudication.decision_fingerprint:
            supporters = {
                row.reviewer
                for row in review.review_submissions
                if row.decision_fingerprint == adjudication.decision_fingerprint
            }
            if len(supporters) >= 2:
                _finalize_component_review(
                    review,
                    adjudication.decision,
                    adjudication.groups_json,
                    "Adjudicated",
                )
            else:
                review.review_status = "Positive Confirmation Required"
        else:
            review.review_status = "Needs Adjudication"
            review.final_decision = ""
            review.final_groups_json = ""
    elif decision == "Unsure":
        review.review_status = "Needs Adjudication"
    elif len(ordinary) + 1 < 2:
        review.review_status = "Partially Reviewed"
    else:
        fingerprints = {row.decision_fingerprint for row in ordinary}
        fingerprints.add(submission.decision_fingerprint)
        if len(fingerprints) == 1:
            _finalize_component_review(
                review, decision, submission.groups_json, "Agreed"
            )
        else:
            review.review_status = "Needs Adjudication"
    review.save(ignore_permissions=True)
    from db_connector.api_identity_human import materialize_final_component_if_enabled

    materialization = materialize_final_component_if_enabled(review.name)
    _refresh_review_workflow_counts(review.canary_run)
    frappe.db.commit()
    return {
        "review": review.name,
        "status": review.review_status,
        "materialization_status": materialization.get("status", "Not Final"),
    }


@frappe.whitelist()
def adjudicate_component_review(
    review_name: str,
    decision: str,
    same_pairs_json: str = "[]",
    notes: str = "",
) -> dict[str, str]:
    _require_manager()
    if decision not in FINAL_COMPONENT_DECISIONS:
        frappe.throw("Adjudication must be All Same, Partial Match, or All Different")
    if not str(notes or "").strip():
        frappe.throw("Adjudication notes are required")
    review = frappe.get_doc(COMPONENT_REVIEW_DOCTYPE, review_name)
    if _component_stale(review):
        frappe.throw("This component is stale. Create a new canary before adjudicating it.")
    if review.review_status != "Needs Adjudication":
        frappe.throw("Only a component awaiting adjudication may be adjudicated")
    _recommendations, record_ids, _expected = _component_context(review)
    _aliases, aliases_to_records = _component_aliases(record_ids)
    groups = _groups_for_component_decision(
        decision, record_ids, aliases_to_records, same_pairs_json
    )
    submission = _append_component_submission(
        review, decision, groups, notes, is_adjudication=True
    )
    supporters = {
        row.reviewer
        for row in review.review_submissions
        if row.decision_fingerprint == submission.decision_fingerprint
    }
    if decision in {"All Same", "Partial Match"} and len(supporters) < 2:
        review.review_status = "Positive Confirmation Required"
        review.final_decision = ""
        review.final_groups_json = ""
    else:
        _finalize_component_review(
            review, decision, submission.groups_json, "Adjudicated"
        )
    review.save(ignore_permissions=True)
    from db_connector.api_identity_human import materialize_final_component_if_enabled

    materialization = materialize_final_component_if_enabled(review.name)
    _refresh_review_workflow_counts(review.canary_run)
    frappe.db.commit()
    return {
        "review": review.name,
        "status": review.review_status,
        "materialization_status": materialization.get("status", "Not Final"),
    }


def _update_qc_review_state(recommendation: Any) -> None:
    ordinary = [row for row in recommendation.qc_review_labels if not row.is_adjudication]
    adjudications = [row for row in recommendation.qc_review_labels if row.is_adjudication]
    if adjudications:
        adjudication = adjudications[-1]
        supporters = {
            row.reviewer
            for row in recommendation.qc_review_labels
            if row.label == adjudication.label
        }
        if adjudication.label == "Same" and len(supporters) < 2:
            recommendation.qc_review_status = "Positive Confirmation Required"
            recommendation.qc_final_label = ""
        else:
            recommendation.qc_review_status = "Adjudicated"
            recommendation.qc_final_label = adjudication.label
            if not recommendation.qc_finalized_at:
                recommendation.qc_finalized_at = frappe.utils.now_datetime()
        return
    labels = [row.label for row in ordinary]
    if "Unsure" in labels:
        recommendation.qc_review_status = "Needs Adjudication"
    elif len(labels) < 2:
        recommendation.qc_review_status = "Partially Reviewed"
    elif len(set(labels)) == 1:
        recommendation.qc_review_status = "Agreed"
        recommendation.qc_final_label = labels[0]
        if not recommendation.qc_finalized_at:
            recommendation.qc_finalized_at = frappe.utils.now_datetime()
    else:
        recommendation.qc_review_status = "Needs Adjudication"


def _locked_qc_recommendation(recommendation_name: str) -> Any:
    """Use the same Run -> Recommendation lock order as QC assignment."""
    from db_connector.api_identity_qc import _lock_named_rows

    run_name = frappe.db.get_value(
        RECOMMENDATION_DOCTYPE, recommendation_name, "canary_run"
    )
    if not run_name:
        frappe.throw("The QC recommendation no longer exists")
    _lock_named_rows(RUN_DOCTYPE, (str(run_name),))
    _lock_named_rows(RECOMMENDATION_DOCTYPE, (recommendation_name,))
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if str(recommendation.canary_run) != str(run_name):
        frappe.throw("The QC recommendation changed while it was being locked")
    return recommendation


@frappe.whitelist()
def submit_recommendation_qc(
    recommendation_name: str, label: str, notes: str = ""
) -> dict[str, str]:
    _require_reviewer()
    if label not in {"Same", "Different", "Unsure"}:
        frappe.throw("QC label must be Same, Different, or Unsure")
    recommendation = _locked_qc_recommendation(recommendation_name)
    if not recommendation.qc_selected:
        frappe.throw("This recommendation is not in the random QC sample")
    if not recommendation.qc_assigned_at:
        frappe.throw(
            "This QC case has not been released by a manager or the governed QC cadence"
        )
    if _recommendation_stale(recommendation):
        recommendation.db_set(
            {"qc_stale": 1, "qc_review_status": "Stale"}, update_modified=False
        )
        frappe.throw("This recommendation is stale. Review it in a new canary.")
    if recommendation.qc_review_status not in OPEN_REVIEW_STATUSES:
        frappe.throw("This QC case is closed to ordinary review")
    ordinary = [row for row in recommendation.qc_review_labels if not row.is_adjudication]
    if any(row.reviewer == frappe.session.user for row in ordinary):
        frappe.throw("Your immutable QC review is already recorded")
    recommendation.append(
        "qc_review_labels",
        {
            "reviewer": frappe.session.user,
            "label": label,
            "notes": str(notes or "").strip(),
            "submitted_at": frappe.utils.now_datetime(),
            "is_adjudication": 0,
        },
    )
    adjudications = [
        row for row in recommendation.qc_review_labels if row.is_adjudication
    ]
    if adjudications and label != adjudications[-1].label:
        recommendation.qc_review_status = "Needs Adjudication"
        recommendation.qc_final_label = ""
    else:
        _update_qc_review_state(recommendation)
    recommendation.save(ignore_permissions=True)
    _refresh_review_workflow_counts(recommendation.canary_run)
    from db_connector.api_identity_qc import refresh_qc_monitor

    refresh_qc_monitor(recommendation.canary_run)
    frappe.db.commit()
    return {
        "recommendation": recommendation.name,
        "status": recommendation.qc_review_status,
    }


@frappe.whitelist()
def adjudicate_recommendation_qc(
    recommendation_name: str, label: str, notes: str = ""
) -> dict[str, str]:
    _require_manager()
    if label not in {"Same", "Different"}:
        frappe.throw("QC adjudication must be Same or Different")
    if not str(notes or "").strip():
        frappe.throw("Adjudication notes are required")
    recommendation = _locked_qc_recommendation(recommendation_name)
    if _recommendation_stale(recommendation):
        frappe.throw("This recommendation is stale. Review it in a new canary.")
    if recommendation.qc_review_status != "Needs Adjudication":
        frappe.throw("Only QC cases awaiting adjudication may be adjudicated")
    recommendation.append(
        "qc_review_labels",
        {
            "reviewer": frappe.session.user,
            "label": label,
            "notes": str(notes).strip(),
            "submitted_at": frappe.utils.now_datetime(),
            "is_adjudication": 1,
        },
    )
    _update_qc_review_state(recommendation)
    recommendation.save(ignore_permissions=True)
    _refresh_review_workflow_counts(recommendation.canary_run)
    from db_connector.api_identity_qc import refresh_qc_monitor

    refresh_qc_monitor(recommendation.canary_run)
    frappe.db.commit()
    return {
        "recommendation": recommendation.name,
        "status": recommendation.qc_review_status,
    }


def _change_recommendation_status(
    recommendation: Any,
    to_status: str,
    event_type: str,
    reason: str,
    *,
    approved: bool = False,
) -> None:
    from_status = str(recommendation.status)
    values: dict[str, Any] = {"status": to_status}
    now = frappe.utils.now_datetime()
    actor = frappe.session.user or "Administrator"
    if approved:
        values.update(
            {
                "approved_at": now,
                "approved_by": actor,
                # Retain the original columns for backward-compatible reports.
                "activated_at": now,
                "activated_by": actor,
            }
        )
    if to_status in {"Withdrawn", "Reversed", "Superseded"}:
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
def approve_canary_recommendations(run_name: str) -> dict[str, Any]:
    """Retired status-only approval endpoint retained for explicit safety."""
    _require_manager()
    frappe.throw(
        "Status-only recommendation approval has been retired. Use Preview Approve All and an approved CCD Identity Activation Batch."
    )
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Ready":
        frappe.throw("Only a Ready canary may have its recommendations approved")
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
                "stale_before_recommendation_approval",
            )
        _refresh_run_counts(run.name)
        _initialize_review_workflow(run.name)
        frappe.db.commit()
        return {
            "run": run.name,
            "status": "Ready",
            "approved": 0,
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
            "Approved",
            "recommendation_only_approval",
            approved=True,
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
            "approved_at": frappe.utils.now_datetime(),
            "approved_by": frappe.session.user,
            "activated_at": frappe.utils.now_datetime(),
            "activated_by": frappe.session.user,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "run": run.name,
        "status": "Active",
        "approved": counts["active_count"],
        "activated": counts["active_count"],
    }


@frappe.whitelist()
def activate_canary(run_name: str) -> dict[str, Any]:
    """Retired legacy alias; it must never bypass the governed materializer."""
    _require_manager()
    frappe.throw(
        "Legacy canary activation is disabled. Use a governed CCD Identity Activation Batch."
    )


@frappe.whitelist()
def reverse_recommendation(recommendation_name: str, reason: str) -> dict[str, str]:
    _require_manager()
    if not str(reason or "").strip():
        frappe.throw("A reversal reason is required")
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if recommendation.status != "Proposed" or recommendation.identity_decision:
        frappe.throw(
            "Only an unmaterialized Proposed recommendation may be withdrawn here. End or supersede Identity Memberships through the Identity Resolution workflow."
        )
    _change_recommendation_status(
        recommendation,
        "Withdrawn",
        "Withdrawn",
        str(reason).strip(),
    )
    _refresh_run_counts(recommendation.canary_run)
    frappe.db.commit()
    return {"recommendation": recommendation.name, "status": "Withdrawn"}


@frappe.whitelist()
def reverse_canary(run_name: str, reason: str) -> dict[str, Any]:
    _require_manager()
    frappe.throw(
        "Bulk status reversal is retired because approved recommendations may own live Identity Memberships. End or supersede memberships through the Identity Resolution workflow."
    )


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
        "exception_component_count": run.exception_component_count,
        "exception_review_complete_count": run.exception_review_complete_count,
        "qc_sample_count": run.qc_sample_count,
        "qc_review_complete_count": run.qc_review_complete_count,
        "summary": json.loads(run.summary_json or "{}"),
    }
