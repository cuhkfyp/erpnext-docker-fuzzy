"""Read-only governed experiments for Splink training-population size.

The approved adapter, evaluation records, and Review queue remain unchanged.
This module reproduces an approved frozen Threshold Evaluation, trains shadow
models with explicitly requested record limits, scores exactly the same human-
labeled pairs, and returns sanitized aggregate metrics only.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from statistics import median
from typing import Any

import frappe

from db_connector.api_fuzzy_evaluation import (
    THRESHOLD_EVALUATION,
    _bounded_probability_records,
    _calibrate_scores,
    _canonical_record,
)
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.splink_adapter import (
    SPLINK_ADAPTER_VERSION,
    dependency_versions,
    score_requested_pairs,
)

EXPERIMENT_VERSION = "pilot-splink-training-size-1.0"
DEFAULT_TRAINING_SIZES = (5_000, 20_000)
MAX_EXPERIMENT_TRAINING_RECORDS = 100_000
DEFAULT_EXPERIMENT_TRAINING_PAIR_BUDGET = 250_000
MAX_EXPERIMENT_TRAINING_PAIR_BUDGET = 1_000_000
DEFAULT_EXPERIMENT_U_PAIR_BUDGET = 250_000


def _ordered_pair(left: Any, right: Any) -> tuple[str, str]:
    return tuple(sorted((str(left), str(right))))


def _parse_training_sizes(value: str | list[int] | tuple[int, ...] | None) -> tuple[int, ...]:
    if value is None or value == "":
        sizes = DEFAULT_TRAINING_SIZES
    elif isinstance(value, str):
        sizes = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    else:
        sizes = tuple(int(item) for item in value)
    sizes = tuple(sorted(set(sizes)))
    if not sizes:
        frappe.throw("At least one Splink training size is required")
    if sizes[0] < 1_000 or sizes[-1] > MAX_EXPERIMENT_TRAINING_RECORDS:
        frappe.throw(
            f"Training sizes must be between 1000 and {MAX_EXPERIMENT_TRAINING_RECORDS}"
        )
    return sizes


def _approved_threshold_evaluation(evaluation_name: str | None = None) -> Any:
    if evaluation_name:
        run = frappe.get_doc("CCD Match Evaluation Run", evaluation_name)
    else:
        rows = frappe.get_all(
            "CCD Match Evaluation Run",
            filters={
                "run_purpose": THRESHOLD_EVALUATION,
                "status": "Completed",
                "approval_status": "Approved",
            },
            pluck="name",
            order_by="modified desc",
            limit=1,
        )
        if not rows:
            frappe.throw("No approved Threshold Evaluation is available")
        run = frappe.get_doc("CCD Match Evaluation Run", rows[0])
    if (
        (run.run_purpose or THRESHOLD_EVALUATION) != THRESHOLD_EVALUATION
        or run.status != "Completed"
        or run.approval_status != "Approved"
    ):
        frappe.throw("The experiment requires an approved completed Threshold Evaluation")
    return run


def _frozen_records(run: Any, policy: MatchingPolicy) -> list[dict[str, Any]]:
    sources = policy.sources()
    placeholders = ", ".join(["%s"] * len(sources))
    raw_rows = frappe.db.sql(
        f"""SELECT * FROM `tabCCD Master`
              WHERE modified <= %s
                AND ccd_reg_source IN ({placeholders})""",
        (run.snapshot_at, *sources),
        as_dict=True,
    )
    stale_snapshot_records = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM `tabCCD Master`
                  WHERE creation <= %s
                    AND modified > %s
                    AND ccd_reg_source IN ({placeholders})""",
            (run.snapshot_at, run.snapshot_at, *sources),
        )[0][0]
    )
    if stale_snapshot_records or len(raw_rows) != int(run.record_count or 0):
        frappe.throw("The approved evaluation snapshot is no longer reproducible")
    return [_canonical_record(dict(row), policy) for row in raw_rows]


def _finalized_pairs(run_name: str) -> list[Any]:
    pairs = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={
            "evaluation_run": run_name,
            "stale": 0,
            "final_label": ["in", ["Same", "Different"]],
        },
        fields=[
            "name",
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
            "final_label",
            "probabilistic_score",
            "probabilistic_available",
        ],
        order_by="name",
        limit_page_length=10_000,
    )
    if not pairs:
        frappe.throw("The approved evaluation has no finalized labeled pairs")
    return pairs


def _verify_pair_endpoints(
    pairs: list[Any],
    record_by_id: dict[str, dict[str, Any]],
) -> set[str]:
    required = set()
    expected: dict[str, str] = {}
    pair_keys = set()
    for pair in pairs:
        key = _ordered_pair(pair.left_record, pair.right_record)
        if key in pair_keys:
            frappe.throw("The approved evaluation contains a duplicate labeled pair")
        pair_keys.add(key)
        required.update(key)
        expected[str(pair.left_record)] = str(pair.left_modified_at or "")
        expected[str(pair.right_record)] = str(pair.right_modified_at or "")
    stale = [
        record_id
        for record_id in required
        if record_id not in record_by_id
        or str(record_by_id[record_id].get("source_modified") or "")
        != expected.get(record_id, "")
    ]
    if stale:
        frappe.throw("A labeled endpoint changed after the approved evaluation snapshot")
    return required


def _average_precision(labels_scores: list[tuple[bool, float]]) -> float:
    positives = sum(label for label, _score in labels_scores)
    if not positives:
        return 0.0
    ranked = sorted(labels_scores, key=lambda item: -item[1])
    true_positives = 0
    total = 0.0
    for rank, (label, _score) in enumerate(ranked, 1):
        if label:
            true_positives += 1
            total += true_positives / rank
    return total / positives


def _roc_auc(labels_scores: list[tuple[bool, float]]) -> float | None:
    positives = sum(label for label, _score in labels_scores)
    negatives = len(labels_scores) - positives
    if not positives or not negatives:
        return None
    ordered = sorted(enumerate(labels_scores), key=lambda item: item[1][1])
    ranks = [0.0] * len(labels_scores)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1][1] == ordered[index][1][1]:
            end += 1
        average_rank = (index + 1 + end) / 2
        for position in range(index, end):
            ranks[ordered[position][0]] = average_rank
        index = end
    positive_rank_sum = sum(
        rank for rank, (label, _score) in zip(ranks, labels_scores) if label
    )
    return (
        positive_rank_sum - positives * (positives + 1) / 2
    ) / (positives * negatives)


def _ranking_metrics(
    pairs: list[Any],
    score_by_pair: dict[tuple[str, str], float],
) -> dict[str, Any]:
    rows = [
        (
            pair.final_label == "Same",
            float(score_by_pair[_ordered_pair(pair.left_record, pair.right_record)]),
        )
        for pair in pairs
        if _ordered_pair(pair.left_record, pair.right_record) in score_by_pair
    ]
    ranked = sorted(rows, key=lambda item: -item[1])
    output: dict[str, Any] = {
        "scored_pairs": len(rows),
        "same_pairs": sum(label for label, _score in rows),
        "average_precision": _average_precision(rows),
        "roc_auc": _roc_auc(rows),
    }
    for requested_k in (30, 50, 100):
        k = min(requested_k, len(ranked))
        same = sum(label for label, _score in ranked[:k])
        output[f"top_{requested_k}"] = {
            "evaluated": k,
            "same": same,
            "precision": same / k if k else None,
        }
    return output


def _threshold_metrics(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "validation_ready": value.get("validation_ready"),
        "automatic_high_threshold": value.get("high_threshold"),
        "review_threshold": value.get("review_threshold"),
        "calibration_pairs": value.get("calibration_pairs"),
        "held_out_pairs": value.get("held_out_pairs"),
        "calibration_positives": value.get("calibration_positives"),
        "held_out_positives": value.get("held_out_positives"),
        "calibration_high": value.get("calibration_high"),
        "held_out_high": value.get("held_out_high"),
        "calibration_review": value.get("calibration_review"),
        "held_out_review": value.get("held_out_review"),
        "warning": value.get("warning"),
    }


def _source_distribution(training_records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = sorted(Counter(str(row.get("source") or "") for row in training_records).values())
    return {
        "source_count": len(counts),
        "minimum_records_per_source": counts[0] if counts else 0,
        "median_records_per_source": median(counts) if counts else 0,
        "maximum_records_per_source": counts[-1] if counts else 0,
    }


def _experiment_pair_rows(
    pairs: list[Any],
    score_by_pair: dict[tuple[str, str], float],
) -> list[Any]:
    return [
        frappe._dict(
            name=pair.name,
            final_label=pair.final_label,
            probabilistic_score=score_by_pair[
                _ordered_pair(pair.left_record, pair.right_record)
            ],
            probabilistic_available=1,
        )
        for pair in pairs
    ]


def run_training_size_experiment(
    evaluation_name: str | None = None,
    training_sizes: str | list[int] | tuple[int, ...] | None = None,
    training_pair_budget: int = DEFAULT_EXPERIMENT_TRAINING_PAIR_BUDGET,
    u_pair_budget: int = DEFAULT_EXPERIMENT_U_PAIR_BUDGET,
) -> dict[str, Any]:
    """Bench-only read-only comparison; returns no identifiers or source labels."""
    sizes = _parse_training_sizes(training_sizes)
    training_pair_budget = int(training_pair_budget)
    if not 10_000 <= training_pair_budget <= MAX_EXPERIMENT_TRAINING_PAIR_BUDGET:
        frappe.throw(
            "The experimental training-pair budget must be between 10000 and "
            f"{MAX_EXPERIMENT_TRAINING_PAIR_BUDGET}"
        )
    u_pair_budget = int(u_pair_budget)
    if not 10_000 <= u_pair_budget <= MAX_EXPERIMENT_TRAINING_PAIR_BUDGET:
        frappe.throw(
            "The experimental u-pair budget must be between 10000 and "
            f"{MAX_EXPERIMENT_TRAINING_PAIR_BUDGET}"
        )
    run = _approved_threshold_evaluation(evaluation_name)
    policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
    records = _frozen_records(run, policy)
    record_by_id = {
        str(record["record_id"]): record
        for record in records
        if record.get("record_id")
    }
    pairs = _finalized_pairs(run.name)
    required_ids = _verify_pair_endpoints(pairs, record_by_id)
    requested_pairs = {
        _ordered_pair(pair.left_record, pair.right_record) for pair in pairs
    }
    scoring_records = [record_by_id[record_id] for record_id in sorted(required_ids)]

    existing_scores = {
        _ordered_pair(pair.left_record, pair.right_record): float(
            pair.probabilistic_score
        )
        for pair in pairs
        if pair.probabilistic_available
    }
    existing_calibration = _calibrate_scores(
        pairs,
        "probabilistic_score",
        policy,
    )
    output: dict[str, Any] = {
        "experiment_version": EXPERIMENT_VERSION,
        "approved_adapter_unchanged": SPLINK_ADAPTER_VERSION,
        "policy_version": policy.version,
        "production_writes": False,
        "snapshot_reproducible": True,
        "governed_record_count": len(records),
        "labeled_pair_count": len(pairs),
        "same_label_count": sum(pair.final_label == "Same" for pair in pairs),
        "different_label_count": sum(
            pair.final_label == "Different" for pair in pairs
        ),
        "unique_labeled_endpoint_count": len(required_ids),
        "experimental_scoring_record_count": len(scoring_records),
        "dependencies": dependency_versions(),
        "experimental_training_pair_budget": training_pair_budget,
        "experimental_u_pair_budget": u_pair_budget,
        "approved_v1_1_baseline": {
            "ranking": _ranking_metrics(pairs, existing_scores),
            "thresholds": _threshold_metrics(existing_calibration),
        },
        "training_size_results": {},
    }

    for size in sizes:
        started = time.monotonic()
        training_records = _bounded_probability_records(
            records,
            required_ids,
            limit=size,
        )
        predictions = score_requested_pairs(
            training_records,
            scoring_records,
            requested_pairs,
            minimum_probability=0.0,
            max_block_size=policy.max_block_size,
            max_prediction_pairs=min(
                policy.max_candidate_pairs,
                training_pair_budget,
            ),
            u_random_max_pairs=u_pair_budget,
        )
        score_by_pair = {
            _ordered_pair(prediction.left_id, prediction.right_id): float(
                prediction.probability
            )
            for prediction in predictions
        }
        if set(score_by_pair) != requested_pairs:
            frappe.throw(
                "The experimental model did not score every governed labeled pair"
            )
        experiment_rows = _experiment_pair_rows(pairs, score_by_pair)
        calibration = _calibrate_scores(
            experiment_rows,
            "probabilistic_score",
            policy,
        )
        output["training_size_results"][str(size)] = {
            "requested_training_records": size,
            "actual_training_records": len(training_records),
            "training_source_distribution": _source_distribution(training_records),
            "elapsed_seconds": time.monotonic() - started,
            "ranking": _ranking_metrics(pairs, score_by_pair),
            "thresholds": _threshold_metrics(calibration),
            "score_minimum": min(score_by_pair.values()),
            "score_median": median(score_by_pair.values()),
            "score_maximum": max(score_by_pair.values()),
        }
    return output
