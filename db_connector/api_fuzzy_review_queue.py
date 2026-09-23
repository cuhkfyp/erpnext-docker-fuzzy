"""Optional human Review queue ranked by the approved Splink maximum-F1 cutoff.

This module never promotes a probabilistic score to model High, never modifies
CCD Master, and never creates an identity link. It stores only versioned Review
candidates and independent human decisions in dedicated DocTypes.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from collections import Counter
from typing import Any

import frappe

from db_connector.api_fuzzy_evaluation import (
    REVIEW_ROLE,
    SENSITIVE_ROLE,
    _bounded_probability_records,
    _evaluation_records,
    _release_unused_memory,
    _splink_record,
)
from db_connector.fuzzy_matching.blocking import (
    BLOCKING_VERSION,
    generate_candidate_pairs,
)
from db_connector.fuzzy_matching.models import build_evidence
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.generation import supersede_prior_queue_generations
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.security import mask_identifier
from db_connector.fuzzy_matching.splink_adapter import (
    RANDOM_MATCH_PRIOR,
    REQUESTED_PAIR_BATCH_SIZE,
    SPLINK_ADAPTER_VERSION,
    U_RANDOM_SEED,
    available,
    dependency_versions,
    score_requested_pairs,
)

RUN_DOCTYPE = "CCD Match Review Queue Run"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
CANARY_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
RUNNING_STATUSES = (
    "Queued",
    "Profiling",
    "Generating Candidates",
    "Training and Scoring Splink",
    "Writing Review Queue",
)
OPEN_REVIEW_STATUSES = {
    "Unreviewed",
    "Partially Reviewed",
    "Positive Confirmation Required",
}
FINAL_REVIEW_STATUSES = {"Agreed", "Adjudicated"}
PROBABILITY_REPLAY_TOLERANCE = 1e-9


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_reviewer() -> None:
    roles = set(frappe.get_roles())
    if "System Manager" not in roles and REVIEW_ROLE not in roles and SENSITIVE_ROLE not in roles:
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _has_sensitive_access() -> bool:
    roles = set(frappe.get_roles())
    return "System Manager" in roles or SENSITIVE_ROLE in roles


def _ordered_pair(left: Any, right: Any) -> tuple[str, str]:
    return tuple(sorted((str(left), str(right))))


def _pair_fingerprint(policy_version: str, left: str, right: str) -> str:
    pair = _ordered_pair(left, right)
    return hashlib.sha256(
        f"{policy_version}\x1f{pair[0]}\x1f{pair[1]}".encode()
    ).hexdigest()


def _queue_pair_key(run_name: str, left: str, right: str) -> str:
    pair = _ordered_pair(left, right)
    return hashlib.sha256(f"{run_name}\x1f{pair[0]}\x1f{pair[1]}".encode()).hexdigest()


def _set_status(run: Any, status: str) -> None:
    run.db_set("status", status, update_modified=False)
    frappe.db.commit()


def _review_threshold_from_run(evaluation: Any) -> float:
    metrics = json.loads(evaluation.metrics_json or "{}")
    splink = (metrics.get("models") or {}).get("fellegi_sunter_calibration") or {}
    threshold = splink.get("review_threshold")
    if not splink.get("validation_ready") or threshold is None:
        frappe.throw("The approved evaluation has no valid Splink Review cutoff")
    return float(threshold)


def _approved_splink_runtime(evaluation: Any) -> dict[str, int]:
    """Return the frozen training limits that produced an approved cutoff.

    A probability cutoff is meaningful only for the fitted model that produced
    it.  The full-population queue therefore fails closed unless the installed
    adapter, dependencies, prior, and resource limits match the approved
    evaluation exactly.
    """
    versions = json.loads(evaluation.model_versions_json or "{}")
    if versions.get("splink_adapter") != SPLINK_ADAPTER_VERSION:
        frappe.throw("The approved cutoff belongs to a different Splink adapter version")
    if versions.get("splink_status") != "local" or versions.get("splink_warning"):
        frappe.throw("The approved evaluation did not complete with the pinned local Splink runtime")
    if versions.get("splink") != dependency_versions():
        frappe.throw("The installed Splink dependencies differ from the approved evaluation")
    if abs(float(versions.get("splink_random_match_prior") or 0) - RANDOM_MATCH_PRIOR) > 1e-15:
        frappe.throw("The approved evaluation used a different Splink random-match prior")

    fields = {
        "training_record_limit": "splink_training_record_limit",
        "training_candidate_pair_limit": "splink_training_candidate_pair_limit",
        "u_random_pair_limit": "splink_u_random_pair_limit",
        "u_random_seed": "splink_u_random_seed",
    }
    runtime: dict[str, int] = {}
    for output_name, version_name in fields.items():
        try:
            value = int(versions.get(version_name))
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            frappe.throw(
                f"The approved evaluation is missing frozen Splink setting {version_name}"
            )
        runtime[output_name] = value
    return runtime


def _queue_splink_records(
    records: list[dict[str, Any]],
    policy: MatchingPolicy,
    training_ids: set[str],
    requested_pairs: set[tuple[str, str]],
    *,
    training_record_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the same normalized model inputs used by threshold evaluation."""
    training = [
        _splink_record(record, policy)
        for record in _bounded_probability_records(
            records,
            training_ids,
            limit=training_record_limit,
        )
    ]
    scoring_ids = {
        record_id
        for pair in requested_pairs
        for record_id in pair
    }
    scoring = [
        _splink_record(record, policy)
        for record in records
        if str(record.get("record_id") or "") in scoring_ids
    ]
    return training, scoring


def _replay_approved_probability_scores(
    evaluation_name: str,
    records: list[dict[str, Any]],
    policy: MatchingPolicy,
    training_records: list[dict[str, Any]],
    splink_runtime: dict[str, int],
    *,
    enforce: bool = True,
) -> dict[str, Any]:
    """Fail closed unless the approved evaluation scores are reproducible."""
    rows = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={
            "evaluation_run": evaluation_name,
            "probabilistic_available": 1,
        },
        fields=["left_record", "right_record", "probabilistic_score"],
        limit_page_length=10_000,
    )
    expected = {
        _ordered_pair(row.left_record, row.right_record): float(
            row.probabilistic_score
        )
        for row in rows
    }
    if not expected:
        frappe.throw("The approved evaluation has no probabilistic scores to replay")
    scoring_ids = {
        record_id
        for pair in expected
        for record_id in pair
    }
    scoring_records = [
        _splink_record(record, policy)
        for record in records
        if str(record.get("record_id") or "") in scoring_ids
    ]
    predictions = score_requested_pairs(
        training_records,
        scoring_records,
        set(expected),
        minimum_probability=-1.0,
        max_block_size=policy.max_block_size,
        max_prediction_pairs=min(
            policy.max_candidate_pairs,
            splink_runtime["training_candidate_pair_limit"],
        ),
        u_random_max_pairs=splink_runtime["u_random_pair_limit"],
        u_random_seed=splink_runtime["u_random_seed"],
    )
    actual = {
        _ordered_pair(item.left_id, item.right_id): float(item.probability)
        for item in predictions
    }
    if set(actual) != set(expected):
        frappe.throw(
            "The approved Splink evaluation replay did not return the same pair set"
        )
    differences = sorted(
        abs(actual[pair] - expected[pair])
        for pair in expected
    )
    max_difference = differences[-1]
    within_tolerance = sum(
        difference <= PROBABILITY_REPLAY_TOLERANCE
        for difference in differences
    )
    result = {
        "pair_count": len(expected),
        "within_tolerance_count": within_tolerance,
        "mean_absolute_difference": sum(differences) / len(differences),
        "median_absolute_difference": differences[len(differences) // 2],
        "max_absolute_difference": max_difference,
        "tolerance": PROBABILITY_REPLAY_TOLERANCE,
        "passed": within_tolerance == len(expected),
    }
    if enforce and not result["passed"]:
        frappe.throw(
            "The approved Splink evaluation scores are not reproducible; "
            f"maximum absolute difference {max_difference:.12g}; "
            "a fresh evaluation is required"
        )
    return result


def _queue_prerequisites(canary_name: str) -> dict[str, Any]:
    canary = frappe.get_doc(CANARY_DOCTYPE, canary_name)
    if canary.status not in {"Ready", "Active"}:
        frappe.throw("The Tiered High canary must be Ready or Active")
    evaluation = frappe.get_doc(
        "CCD Match Evaluation Run", canary.threshold_evaluation_run
    )
    if evaluation.status != "Completed" or evaluation.approval_status != "Approved":
        frappe.throw("The Splink threshold evaluation is no longer approved")
    if evaluation.candidate_truncated or json.loads(evaluation.skipped_blocks_json or "[]"):
        frappe.throw(
            "The approved threshold evaluation did not use complete general candidate generation"
        )
    threshold = _review_threshold_from_run(evaluation)
    if abs(threshold - float(canary.splink_review_threshold or 0)) > 1e-12:
        frappe.throw("The canary and approved evaluation use different Review cutoffs")
    if not available():
        frappe.throw("The pinned local Splink dependencies are unavailable")
    splink_runtime = _approved_splink_runtime(evaluation)
    return {
        "canary": canary,
        "evaluation": evaluation,
        "threshold": threshold,
        "splink_runtime": splink_runtime,
    }


def _create_queue_run(
    canary_name: str,
    *,
    replacement_for: str = "",
) -> dict[str, str]:
    prerequisites = _queue_prerequisites(canary_name)
    canary = prerequisites["canary"]
    existing = frappe.get_all(
        RUN_DOCTYPE,
        filters={"canary_run": canary.name, "status": ["in", list(RUNNING_STATUSES) + ["Ready"]]},
        pluck="name",
        limit_page_length=100,
    )
    replacement_for = str(replacement_for or "")
    if replacement_for:
        replaced = frappe.get_doc(RUN_DOCTYPE, replacement_for)
        if replaced.canary_run != canary.name or replaced.status != "Ready":
            frappe.throw("Only a Ready queue for this canary may be replaced")
    blocking = [name for name in existing if str(name) != replacement_for]
    if blocking:
        frappe.throw(f"Splink Review queue {blocking[0]} already exists for this canary")
    run = frappe.get_doc(
        {
            "doctype": RUN_DOCTYPE,
            "canary_run": canary.name,
            "matching_policy": canary.matching_policy,
            "policy_version": canary.policy_version,
            "policy_snapshot_json": canary.policy_snapshot_json,
            "policy_snapshot_sha256": canary.policy_snapshot_sha256,
            "threshold_evaluation_run": prerequisites["evaluation"].name,
            "splink_adapter_version": SPLINK_ADAPTER_VERSION,
            "review_threshold": prerequisites["threshold"],
            "threshold_objective": "maximum_calibration_f1",
            "snapshot_at": canary.snapshot_at,
            "status": "Queued",
        }
    ).insert(ignore_permissions=True)
    frappe.enqueue(
        "db_connector.api_fuzzy_review_queue.run_review_queue",
        queue="long",
        timeout=28_800,
        enqueue_after_commit=True,
        run_name=run.name,
    )
    frappe.db.commit()
    return {"run": run.name, "status": "Queued"}


@frappe.whitelist()
def enqueue_review_queue(canary_name: str) -> dict[str, str]:
    _require_manager()
    return _create_queue_run(canary_name)


def install_review_queue(canary_name: str) -> dict[str, str]:
    """Bench-only launcher for the optional full-population Review queue."""
    return _create_queue_run(canary_name)


def install_replacement_review_queue(
    canary_name: str,
    replacement_for: str,
) -> dict[str, str]:
    """Bench-only atomic replacement; the prior queue remains until success."""
    return _create_queue_run(canary_name, replacement_for=replacement_for)


def retire_defective_review_queue(
    run_name: str,
    reason: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Bench-only retirement of invalid unhandled work; history is preserved."""
    if not frappe.utils.cint(confirm):
        frappe.throw("Explicit confirm=True is required")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 240:
        frappe.throw("A bounded non-empty defect reason is required")
    frappe.db.sql(
        f"SELECT name FROM `tab{RUN_DOCTYPE}` WHERE name = %s FOR UPDATE",
        (run_name,),
    )
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status == "Stale":
        return {
            "run": run.name,
            "status": "Stale",
            "already_retired": True,
            "retired_unhandled_candidates": 0,
        }
    if run.status != "Ready":
        frappe.throw("Only a Ready defective queue may be retired")
    if run.splink_adapter_version == SPLINK_ADAPTER_VERSION:
        frappe.throw("The queue already uses the current Splink adapter")
    historical_states = ("Applied", "Reversed", "Superseded")
    placeholders = ", ".join(["%s"] * len(historical_states))
    retired_count = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM `tab{CANDIDATE_DOCTYPE}`
                 WHERE queue_run = %s
                   AND materialization_status NOT IN ({placeholders})""",
            (run.name, *historical_states),
        )[0][0]
    )
    preserved_history_count = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM `tab{CANDIDATE_DOCTYPE}`
                 WHERE queue_run = %s
                   AND materialization_status IN ({placeholders})""",
            (run.name, *historical_states),
        )[0][0]
    )
    frappe.db.sql(
        f"""UPDATE `tab{CANDIDATE_DOCTYPE}`
               SET stale = 1,
                   review_status = CASE
                       WHEN review_status IN ('Agreed', 'Adjudicated')
                       THEN review_status ELSE 'Stale' END,
                   materialization_status = 'Superseded'
             WHERE queue_run = %s
               AND materialization_status NOT IN ({placeholders})""",
        (run.name, *historical_states),
    )
    summary = json.loads(run.summary_json or "{}")
    summary["defect_retirement"] = {
        "retired_at": frappe.utils.now_datetime(),
        "reason": reason,
        "retired_unhandled_candidates": retired_count,
        "preserved_historical_candidates": preserved_history_count,
        "replacement_required": True,
    }
    run.db_set(
        {
            "status": "Stale",
            "summary_json": _json(summary),
            "error_summary": reason,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "run": run.name,
        "status": "Stale",
        "already_retired": False,
        "retired_unhandled_candidates": retired_count,
        "preserved_historical_candidates": preserved_history_count,
    }


def validate_review_queue_model_replay(
    canary_name: str,
    diagnose_only: bool = False,
) -> dict[str, Any]:
    """Read-only validation that an approved cutoff model is reproducible."""
    prerequisites = _queue_prerequisites(canary_name)
    canary = prerequisites["canary"]
    evaluation = prerequisites["evaluation"]
    splink_runtime = prerequisites["splink_runtime"]
    policy = MatchingPolicy.from_dict(json.loads(canary.policy_snapshot_json))
    records = _evaluation_records(policy, canary.snapshot_at)
    if len(records) != int(canary.record_count or 0):
        frappe.throw("The frozen canary record population is no longer reproducible")
    record_by_id = {
        str(record["record_id"]): record
        for record in records
        if record.get("record_id")
    }
    training_ids, stale_training = _threshold_training_context(
        evaluation.name,
        record_by_id,
    )
    if stale_training:
        frappe.throw("The approved Splink training cohort changed; recalibration is required")
    training_records, _scoring_records = _queue_splink_records(
        records,
        policy,
        training_ids,
        set(),
        training_record_limit=splink_runtime["training_record_limit"],
    )
    replay = _replay_approved_probability_scores(
        evaluation.name,
        records,
        policy,
        training_records,
        splink_runtime,
        enforce=not bool(diagnose_only),
    )
    return {
        "canary": canary.name,
        "evaluation": evaluation.name,
        "record_count": len(records),
        "training_record_count": len(training_records),
        "probability_replay": replay,
        "production_records_modified": False,
    }


def validate_current_splink_runtime_repeatability(
    evaluation_name: str,
) -> dict[str, Any]:
    """Read-only two-pass proof for the currently installed adapter."""
    evaluation = frappe.get_doc("CCD Match Evaluation Run", evaluation_name)
    if evaluation.status != "Completed" or evaluation.approval_status != "Approved":
        frappe.throw("Repeatability validation requires an approved completed evaluation")
    policy = MatchingPolicy.from_dict(json.loads(evaluation.policy_snapshot_json))
    records = _evaluation_records(policy, evaluation.snapshot_at)
    if len(records) != int(evaluation.record_count or 0):
        frappe.throw("The frozen evaluation record population is no longer reproducible")
    record_by_id = {
        str(record["record_id"]): record
        for record in records
        if record.get("record_id")
    }
    training_ids, stale_training = _threshold_training_context(
        evaluation.name,
        record_by_id,
    )
    if stale_training:
        frappe.throw("The approved Splink training cohort changed; recalibration is required")
    versions = json.loads(evaluation.model_versions_json or "{}")
    runtime = {
        "training_record_limit": int(
            versions.get("splink_training_record_limit") or 0
        ),
        "training_candidate_pair_limit": int(
            versions.get("splink_training_candidate_pair_limit") or 0
        ),
        "u_random_pair_limit": int(
            versions.get("splink_u_random_pair_limit") or 0
        ),
        "u_random_seed": U_RANDOM_SEED,
    }
    if any(value <= 0 for value in runtime.values()):
        frappe.throw("The historical evaluation is missing its bounded Splink settings")
    rows = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={"evaluation_run": evaluation.name, "stale": 0},
        fields=["left_record", "right_record"],
        limit_page_length=10_000,
    )
    requested = {
        _ordered_pair(row.left_record, row.right_record)
        for row in rows
    }
    training_records, scoring_records = _queue_splink_records(
        records,
        policy,
        training_ids,
        requested,
        training_record_limit=runtime["training_record_limit"],
    )

    def score() -> dict[tuple[str, str], float]:
        predictions = score_requested_pairs(
            training_records,
            scoring_records,
            requested,
            minimum_probability=-1.0,
            max_block_size=policy.max_block_size,
            max_prediction_pairs=min(
                policy.max_candidate_pairs,
                runtime["training_candidate_pair_limit"],
            ),
            u_random_max_pairs=runtime["u_random_pair_limit"],
            u_random_seed=runtime["u_random_seed"],
        )
        return {
            _ordered_pair(item.left_id, item.right_id): float(item.probability)
            for item in predictions
        }

    first = score()
    _release_unused_memory()
    second = score()
    if set(first) != requested or set(second) != requested:
        frappe.throw("Repeatability validation did not score the complete pair set")
    differences = sorted(abs(first[pair] - second[pair]) for pair in requested)
    return {
        "evaluation": evaluation.name,
        "current_splink_adapter": SPLINK_ADAPTER_VERSION,
        "u_random_seed": runtime["u_random_seed"],
        "pair_count": len(requested),
        "within_tolerance_count": sum(
            difference <= PROBABILITY_REPLAY_TOLERANCE
            for difference in differences
        ),
        "max_absolute_difference": differences[-1] if differences else 0,
        "tolerance": PROBABILITY_REPLAY_TOLERANCE,
        "passed": all(
            difference <= PROBABILITY_REPLAY_TOLERANCE
            for difference in differences
        ),
        "production_records_modified": False,
    }


def _human_used_pair_keys() -> set[tuple[str, str]]:
    rows = frappe.db.sql(
        """SELECT pair.left_record, pair.right_record
             FROM `tabCCD Match Evaluation Pair` pair
            WHERE COALESCE(pair.final_label, '') != ''
               OR EXISTS (
                    SELECT 1 FROM `tabCCD Match Review Label` label
                     WHERE label.parent = pair.name
                       AND label.parenttype = 'CCD Match Evaluation Pair'
               )
            UNION
           SELECT candidate.left_record, candidate.right_record
             FROM `tabCCD Match Review Candidate` candidate
            WHERE COALESCE(candidate.final_label, '') != ''
               OR EXISTS (
                    SELECT 1 FROM `tabCCD Match Review Label` queue_label
                     WHERE queue_label.parent = candidate.name
                       AND queue_label.parenttype = 'CCD Match Review Candidate'
               )""",
        as_dict=True,
    )
    return {_ordered_pair(row.left_record, row.right_record) for row in rows}


def _threshold_training_context(
    evaluation_name: str,
    record_by_id: dict[str, dict[str, Any]],
) -> tuple[set[str], int]:
    rows = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={"evaluation_run": evaluation_name},
        fields=[
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
        ],
        limit_page_length=10_000,
    )
    required = set()
    expected: dict[str, str] = {}
    for row in rows:
        required.update((str(row.left_record), str(row.right_record)))
        expected[str(row.left_record)] = str(row.left_modified_at or "")
        expected[str(row.right_record)] = str(row.right_modified_at or "")
    stale = sum(
        1
        for record_id in required
        if record_id not in record_by_id
        or str(record_by_id[record_id].get("source_modified") or "")
        != expected.get(record_id, "")
    )
    return required, stale


def _refresh_review_counts(run_name: str) -> dict[str, int]:
    rows = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={"queue_run": run_name},
        fields=["review_status", "final_label", "count(name) as count"],
        group_by="review_status, final_label",
    )
    complete = same = different = adjudication = 0
    for row in rows:
        count = int(row.count or 0)
        if row.review_status in FINAL_REVIEW_STATUSES:
            complete += count
        if row.final_label == "Same":
            same += count
        elif row.final_label == "Different":
            different += count
        if row.review_status == "Needs Adjudication":
            adjudication += count
    values = {
        "review_complete_count": complete,
        "same_count": same,
        "different_count": different,
        "needs_adjudication_count": adjudication,
    }
    frappe.db.set_value(RUN_DOCTYPE, run_name, values, update_modified=False)
    return values


def _bulk_write_candidates(
    run: Any,
    predictions: list[Any],
    pair_metadata: dict[tuple[str, str], Any],
    record_by_id: dict[str, dict[str, Any]],
) -> None:
    fields = [
        "name",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
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
        "model_tier",
        "probabilistic_score",
        "review_threshold",
        "priority_rank",
        "review_status",
    ]
    now = frappe.utils.now_datetime()
    policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
    values = []
    for rank, prediction in enumerate(predictions, 1):
        pair_key = _ordered_pair(prediction.left_id, prediction.right_id)
        pair = pair_metadata[pair_key]
        left = record_by_id[pair_key[0]]
        right = record_by_id[pair_key[1]]
        values.append(
            (
                frappe.generate_hash(length=10),
                now,
                now,
                "Administrator",
                "Administrator",
                0,
                run.name,
                _queue_pair_key(run.name, *pair_key),
                _pair_fingerprint(run.policy_version, *pair_key),
                pair_key[0],
                pair_key[1],
                left["source"],
                right["source"],
                left["source_modified"],
                right["source_modified"],
                identity_fingerprint(left, policy),
                identity_fingerprint(right, policy),
                pair.source_pair,
                ", ".join(pair.blocking_routes),
                "Review",
                float(prediction.probability),
                float(run.review_threshold),
                rank,
                "Unreviewed",
            )
        )
    frappe.db.bulk_insert(CANDIDATE_DOCTYPE, fields, values, chunk_size=5_000)


def run_review_queue(run_name: str) -> None:
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status != "Queued":
        frappe.throw("Only a queued Splink Review run may execute")
    _set_status(run, "Profiling")
    try:
        policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
        prerequisites = _queue_prerequisites(run.canary_run)
        canary = prerequisites["canary"]
        evaluation = prerequisites["evaluation"]
        splink_runtime = prerequisites["splink_runtime"]
        if (
            evaluation.name != run.threshold_evaluation_run
            or abs(float(prerequisites["threshold"]) - float(run.review_threshold))
            > 1e-12
            or canary.policy_snapshot_sha256 != run.policy_snapshot_sha256
        ):
            frappe.throw("The queued run no longer matches its approved canary provenance")
        sources = policy.sources()
        placeholders = ", ".join(["%s"] * len(sources))
        records = _evaluation_records(policy, run.snapshot_at)
        stale_snapshot_records = int(
            frappe.db.sql(
                f"""SELECT COUNT(*)
                      FROM `tabCCD Master`
                     WHERE creation <= %s
                       AND modified > %s
                       AND ccd_reg_source IN ({placeholders})""",
                (run.snapshot_at, run.snapshot_at, *sources),
            )[0][0]
        )
        run.db_set(
            "snapshot_stale_record_count",
            stale_snapshot_records,
            update_modified=False,
        )
        record_by_id = {
            str(record["record_id"]): record
            for record in records
            if record.get("record_id")
        }
        run.db_set("record_count", len(records), update_modified=False)
        if stale_snapshot_records or len(records) != int(canary.record_count or 0):
            frappe.throw(
                "The frozen canary record population is no longer reproducible"
            )

        _set_status(run, "Generating Candidates")
        blocked = generate_candidate_pairs(records, policy)
        run.db_set("candidate_count", len(blocked.pairs), update_modified=False)
        run.db_set("candidate_truncated", int(blocked.truncated), update_modified=False)
        run.db_set("skipped_blocks_json", _json(blocked.skipped_blocks), update_modified=False)
        if blocked.truncated or blocked.skipped_blocks:
            frappe.throw(
                "Splink Review queue requires complete candidate generation"
            )
        tiered_high = {
            _ordered_pair(row.left_record, row.right_record)
            for row in frappe.get_all(
                RECOMMENDATION_DOCTYPE,
                filters={"canary_run": run.canary_run},
                fields=["left_record", "right_record"],
                limit_page_length=100_000,
            )
        }
        human_used = _human_used_pair_keys()
        requested = set()
        high_excluded = historical_excluded = 0
        for pair in blocked.pairs:
            key = _ordered_pair(pair.left_id, pair.right_id)
            if key in tiered_high:
                high_excluded += 1
            elif key in human_used:
                historical_excluded += 1
            else:
                requested.add(key)
        run.db_set("tiered_high_excluded_count", high_excluded, update_modified=False)
        run.db_set(
            "historical_review_excluded_count",
            historical_excluded,
            update_modified=False,
        )
        run.db_set("eligible_pair_count", len(requested), update_modified=False)

        training_ids, stale_training = _threshold_training_context(
            run.threshold_evaluation_run, record_by_id
        )
        run.db_set(
            "training_cohort_stale_count", stale_training, update_modified=False
        )
        if stale_training:
            frappe.throw(
                "The approved Splink training cohort changed; recalibration is required"
            )
        training_records, scoring_records = _queue_splink_records(
            records,
            policy,
            training_ids,
            requested,
            training_record_limit=splink_runtime["training_record_limit"],
        )
        run.db_set(
            "training_record_count", len(training_records), update_modified=False
        )
        _release_unused_memory()
        _set_status(run, "Training and Scoring Splink")
        probability_replay = _replay_approved_probability_scores(
            evaluation.name,
            records,
            policy,
            training_records,
            splink_runtime,
        )
        _release_unused_memory()
        predictions = score_requested_pairs(
            training_records,
            scoring_records,
            requested,
            minimum_probability=float(run.review_threshold),
            max_block_size=policy.max_block_size,
            max_prediction_pairs=min(
                policy.max_candidate_pairs,
                splink_runtime["training_candidate_pair_limit"],
            ),
            u_random_max_pairs=splink_runtime["u_random_pair_limit"],
            u_random_seed=splink_runtime["u_random_seed"],
        )
        # The batch adapter checks that every requested pair receives exactly
        # one score before filtering. Therefore this count is valid even though
        # only above-cutoff predictions are returned to Python.
        run.db_set("scored_pair_count", len(requested), update_modified=False)
        run.db_set("above_threshold_count", len(predictions), update_modified=False)

        selected_keys = {
            _ordered_pair(prediction.left_id, prediction.right_id)
            for prediction in predictions
        }
        pair_metadata = {}
        for pair in blocked.pairs:
            key = _ordered_pair(pair.left_id, pair.right_id)
            if key in selected_keys:
                pair_metadata[key] = pair
        if len(pair_metadata) != len(selected_keys):
            frappe.throw("An above-cutoff prediction is outside the governed candidates")
        predictions.sort(
            key=lambda prediction: (
                -float(prediction.probability),
                _ordered_pair(prediction.left_id, prediction.right_id),
            )
        )

        _set_status(run, "Writing Review Queue")
        _bulk_write_candidates(run, predictions, pair_metadata, record_by_id)
        source_pair_counts = Counter(
            pair_metadata[_ordered_pair(item.left_id, item.right_id)].source_pair
            for item in predictions
        )
        probabilities = [float(item.probability) for item in predictions]
        summary = {
            "blocking_version": BLOCKING_VERSION,
            "splink_adapter_version": SPLINK_ADAPTER_VERSION,
            "splink_dependencies": dependency_versions(),
            "random_match_prior": RANDOM_MATCH_PRIOR,
            "threshold": float(run.review_threshold),
            "threshold_objective": "maximum_calibration_f1",
            "requested_pair_batch_size": REQUESTED_PAIR_BATCH_SIZE,
            "approved_evaluation_run": evaluation.name,
            "frozen_training_record_limit": splink_runtime[
                "training_record_limit"
            ],
            "frozen_training_candidate_pair_limit": splink_runtime[
                "training_candidate_pair_limit"
            ],
            "frozen_u_random_pair_limit": splink_runtime[
                "u_random_pair_limit"
            ],
            "frozen_u_random_seed": splink_runtime["u_random_seed"],
            "evaluation_model_configuration_replayed": True,
            "evaluation_probability_replay": probability_replay,
            "normalized_splink_records": True,
            "candidate_count": len(blocked.pairs),
            "snapshot_stale_record_count": stale_snapshot_records,
            "tiered_high_excluded_count": high_excluded,
            "historical_review_excluded_count": historical_excluded,
            "eligible_pair_count": len(requested),
            "scored_pair_count": len(requested),
            "above_threshold_count": len(predictions),
            "score_min": min(probabilities) if probabilities else None,
            "score_max": max(probabilities) if probabilities else None,
            "source_pair_counts": dict(sorted(source_pair_counts.items())),
            "automatic_high_enabled": False,
            "production_records_modified": False,
        }
        summary["generation_replacement"] = supersede_prior_queue_generations(run)
        run.db_set("queued_count", len(predictions), update_modified=False)
        run.db_set("summary_json", _json(summary), update_modified=False)
        _refresh_review_counts(run.name)
        run.db_set("status", "Ready", update_modified=False)
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            RUN_DOCTYPE,
            run_name,
            {
                "status": "Failed",
                "error_summary": f"splink_review_queue_failed:{type(exc).__name__}",
            },
            update_modified=False,
        )
        frappe.log_error(traceback.format_exc(), "CCD Splink Review Queue failed")
        frappe.db.commit()
        raise


def _candidate_stale(candidate: Any) -> bool:
    left_modified = frappe.db.get_value("CCD Master", candidate.left_record, "modified")
    right_modified = frappe.db.get_value("CCD Master", candidate.right_record, "modified")
    return (
        str(left_modified or "") != str(candidate.left_modified_at or "")
        or str(right_modified or "") != str(candidate.right_modified_at or "")
    )


def _display_value(value: Any, sensitive: bool) -> str:
    raw = str(value or "").strip()
    return raw if sensitive else mask_identifier(raw, visible_suffix=2)


@frappe.whitelist()
def get_candidate_evidence(candidate_name: str) -> dict[str, Any]:
    _require_reviewer()
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    is_manager = "System Manager" in set(frappe.get_roles())
    run = frappe.get_doc(RUN_DOCTYPE, candidate.queue_run)
    policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
    left_exists = bool(frappe.db.exists("CCD Master", candidate.left_record))
    right_exists = bool(frappe.db.exists("CCD Master", candidate.right_record))
    if not left_exists or not right_exists:
        if not candidate.stale:
            stale_values = {"stale": 1}
            if candidate.review_status not in FINAL_REVIEW_STATUSES:
                stale_values["review_status"] = "Stale"
            frappe.db.set_value(
                CANDIDATE_DOCTYPE,
                candidate.name,
                stale_values,
                update_modified=False,
            )
        return {
            "candidate": candidate.name,
            "model_tier": "Review",
            "left": {"alias": "Left", "source": candidate.left_source},
            "right": {"alias": "Right", "source": candidate.right_source},
            "attributes": [],
            "sensitive_values_visible": False,
            "stale": True,
            "historical_source_retired": True,
            "historical_message": "Historical source retired",
            "review_status": (
                candidate.review_status
                if candidate.review_status in FINAL_REVIEW_STATUSES
                else "Stale"
            ),
            "final_label": candidate.final_label or "",
            "materialization_status": candidate.materialization_status or "Not Final",
            "identity_decision": candidate.identity_decision or "",
            "materialization_error": candidate.materialization_error or "",
            "priority_rank": candidate.priority_rank,
            "can_submit": False,
            "can_adjudicate": False,
            "can_materialize": False,
            "can_reverse_materialization": False,
            **(
                {
                    "probabilistic_score": candidate.probabilistic_score,
                    "review_threshold": candidate.review_threshold,
                    "blocking_routes": candidate.blocking_routes,
                    "correction_decision": candidate.correction_decision or "",
                }
                if is_manager
                else {}
            ),
        }
    left = frappe.get_doc("CCD Master", candidate.left_record).as_dict()
    right = frappe.get_doc("CCD Master", candidate.right_record).as_dict()
    left["source"] = candidate.left_source
    right["source"] = candidate.right_source
    evidence = build_evidence(left, right, policy)
    sensitive = _has_sensitive_access()
    attributes = []
    for attribute in policy.attributes():
        item = evidence.get(attribute)
        attributes.append(
            {
                "attribute": attribute,
                "left": _display_value(policy.value(left, attribute), sensitive),
                "right": _display_value(policy.value(right, attribute), sensitive),
                "comparison": str(item.level.value if item else "not_compared"),
            }
        )
    stale = _candidate_stale(candidate)
    if stale and not candidate.stale:
        stale_values = {"stale": 1}
        if candidate.review_status not in FINAL_REVIEW_STATUSES:
            stale_values["review_status"] = "Stale"
        frappe.db.set_value(
            CANDIDATE_DOCTYPE,
            candidate.name,
            stale_values,
            update_modified=False,
        )
    ordinary = [row for row in candidate.review_labels if not row.is_adjudication]
    submitted = any(row.reviewer == frappe.session.user for row in ordinary)
    payload = {
        "candidate": candidate.name,
        "model_tier": "Review",
        "left": {"alias": "Left", "source": candidate.left_source},
        "right": {"alias": "Right", "source": candidate.right_source},
        "attributes": attributes,
        "sensitive_values_visible": sensitive,
        "stale": stale,
        "review_status": (
            candidate.review_status
            if candidate.review_status in FINAL_REVIEW_STATUSES
            else ("Stale" if stale else candidate.review_status)
        ),
        "final_label": candidate.final_label or "",
        "materialization_status": candidate.materialization_status or "Not Final",
        "identity_decision": candidate.identity_decision or "",
        "materialization_error": candidate.materialization_error or "",
        "priority_rank": candidate.priority_rank,
        "can_submit": bool(
            not stale
            and candidate.review_status in OPEN_REVIEW_STATUSES
            and not submitted
        ),
        "can_adjudicate": bool(
            is_manager
            and not stale
            and candidate.review_status == "Needs Adjudication"
        ),
        "can_materialize": bool(
            is_manager
            and candidate.review_status in FINAL_REVIEW_STATUSES
            and candidate.materialization_status in {"Pending", "Exception"}
        ),
        "can_reverse_materialization": bool(
            is_manager
            and candidate.review_status in FINAL_REVIEW_STATUSES
            and candidate.final_label == "Same"
            and candidate.materialization_status == "Applied"
            and candidate.identity_decision
            and not candidate.correction_decision
        ),
    }
    if sensitive:
        payload["left"]["record_id"] = candidate.left_record
        payload["right"]["record_id"] = candidate.right_record
    if is_manager:
        payload["probabilistic_score"] = candidate.probabilistic_score
        payload["review_threshold"] = candidate.review_threshold
        payload["blocking_routes"] = candidate.blocking_routes
        payload["correction_decision"] = candidate.correction_decision or ""
    return payload


def _update_candidate_review_state(candidate: Any) -> None:
    ordinary = [row for row in candidate.review_labels if not row.is_adjudication]
    adjudications = [row for row in candidate.review_labels if row.is_adjudication]
    if adjudications:
        adjudication = adjudications[-1]
        supporters = {
            row.reviewer
            for row in candidate.review_labels
            if row.label == adjudication.label
        }
        if adjudication.label == "Same" and len(supporters) < 2:
            candidate.review_status = "Positive Confirmation Required"
            candidate.final_label = ""
        else:
            candidate.review_status = "Adjudicated"
            candidate.final_label = adjudication.label
        return
    labels = [row.label for row in ordinary]
    if "Unsure" in labels:
        candidate.review_status = "Needs Adjudication"
    elif not labels:
        candidate.review_status = "Unreviewed"
    elif labels[0] == "Different" and len(labels) == 1:
        candidate.review_status = "Agreed"
        candidate.final_label = "Different"
    elif len(labels) < 2:
        candidate.review_status = "Positive Confirmation Required"
    elif len(set(labels)) == 1:
        candidate.review_status = "Agreed"
        candidate.final_label = labels[0]
    else:
        candidate.review_status = "Needs Adjudication"
        candidate.final_label = ""


@frappe.whitelist()
def submit_candidate_review(
    candidate_name: str, label: str, notes: str = ""
) -> dict[str, str]:
    _require_reviewer()
    if label not in {"Same", "Different", "Unsure"}:
        frappe.throw("Label must be Same, Different, or Unsure")
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if _candidate_stale(candidate):
        candidate.db_set(
            {"stale": 1, "review_status": "Stale"}, update_modified=False
        )
        frappe.throw("This candidate is stale. Generate a new Review queue.")
    if candidate.review_status not in OPEN_REVIEW_STATUSES:
        frappe.throw("This candidate is closed to ordinary review")
    ordinary = [row for row in candidate.review_labels if not row.is_adjudication]
    if any(row.reviewer == frappe.session.user for row in ordinary):
        frappe.throw("Your immutable review is already recorded")
    candidate.append(
        "review_labels",
        {
            "reviewer": frappe.session.user,
            "label": label,
            "notes": str(notes or "").strip(),
            "submitted_at": frappe.utils.now_datetime(),
            "is_adjudication": 0,
        },
    )
    adjudications = [row for row in candidate.review_labels if row.is_adjudication]
    if adjudications and label != adjudications[-1].label:
        candidate.review_status = "Needs Adjudication"
        candidate.final_label = ""
    else:
        _update_candidate_review_state(candidate)
    candidate.save(ignore_permissions=True)
    from db_connector.api_identity_human import materialize_final_candidate_if_enabled

    materialization = materialize_final_candidate_if_enabled(candidate.name)
    from db_connector.api_identity_review_batch import refresh_review_batch_for_candidate

    refresh_review_batch_for_candidate(candidate.name)
    _refresh_review_counts(candidate.queue_run)
    frappe.db.commit()
    return {
        "candidate": candidate.name,
        "status": candidate.review_status,
        "materialization_status": materialization.get("status", "Not Final"),
    }


@frappe.whitelist()
def adjudicate_candidate_review(
    candidate_name: str, label: str, notes: str = ""
) -> dict[str, str]:
    _require_manager()
    if label not in {"Same", "Different"}:
        frappe.throw("Adjudication must be Same or Different")
    if not str(notes or "").strip():
        frappe.throw("Adjudication notes are required")
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if _candidate_stale(candidate):
        frappe.throw("This candidate is stale. Generate a new Review queue.")
    if candidate.review_status != "Needs Adjudication":
        frappe.throw("Only candidates awaiting adjudication may be adjudicated")
    candidate.append(
        "review_labels",
        {
            "reviewer": frappe.session.user,
            "label": label,
            "notes": str(notes).strip(),
            "submitted_at": frappe.utils.now_datetime(),
            "is_adjudication": 1,
        },
    )
    _update_candidate_review_state(candidate)
    candidate.save(ignore_permissions=True)
    from db_connector.api_identity_human import materialize_final_candidate_if_enabled

    materialization = materialize_final_candidate_if_enabled(candidate.name)
    from db_connector.api_identity_review_batch import refresh_review_batch_for_candidate

    refresh_review_batch_for_candidate(candidate.name)
    _refresh_review_counts(candidate.queue_run)
    frappe.db.commit()
    return {
        "candidate": candidate.name,
        "status": candidate.review_status,
        "final_label": candidate.final_label or "",
        "materialization_status": materialization.get("status", "Not Final"),
    }


@frappe.whitelist()
def get_queue_summary(run_name: str) -> dict[str, Any]:
    _require_reviewer()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    return {
        "run": run.name,
        "status": run.status,
        "review_threshold": run.review_threshold,
        "threshold_objective": run.threshold_objective,
        "record_count": run.record_count,
        "candidate_count": run.candidate_count,
        "snapshot_stale_record_count": run.snapshot_stale_record_count,
        "eligible_pair_count": run.eligible_pair_count,
        "scored_pair_count": run.scored_pair_count,
        "tiered_high_excluded_count": run.tiered_high_excluded_count,
        "historical_review_excluded_count": run.historical_review_excluded_count,
        "queued_count": run.queued_count,
        "review_complete_count": run.review_complete_count,
        "same_count": run.same_count,
        "different_count": run.different_count,
        "needs_adjudication_count": run.needs_adjudication_count,
    }
