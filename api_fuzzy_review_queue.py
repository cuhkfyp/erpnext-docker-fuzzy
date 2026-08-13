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
    _canonical_record,
)
from db_connector.fuzzy_matching.blocking import (
    BLOCKING_VERSION,
    generate_candidate_pairs,
)
from db_connector.fuzzy_matching.models import build_evidence
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.security import mask_identifier
from db_connector.fuzzy_matching.splink_adapter import (
    RANDOM_MATCH_PRIOR,
    REQUESTED_PAIR_BATCH_SIZE,
    SPLINK_ADAPTER_VERSION,
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


def _queue_prerequisites(canary_name: str) -> dict[str, Any]:
    canary = frappe.get_doc(CANARY_DOCTYPE, canary_name)
    if canary.status not in {"Ready", "Active"}:
        frappe.throw("The Tiered High canary must be Ready or Active")
    evaluation = frappe.get_doc(
        "CCD Match Evaluation Run", canary.threshold_evaluation_run
    )
    if evaluation.status != "Completed" or evaluation.approval_status != "Approved":
        frappe.throw("The Splink threshold evaluation is no longer approved")
    threshold = _review_threshold_from_run(evaluation)
    if abs(threshold - float(canary.splink_review_threshold or 0)) > 1e-12:
        frappe.throw("The canary and approved evaluation use different Review cutoffs")
    versions = json.loads(evaluation.model_versions_json or "{}")
    if versions.get("splink_adapter") != SPLINK_ADAPTER_VERSION:
        frappe.throw("The approved cutoff belongs to a different Splink adapter version")
    if not available():
        frappe.throw("The pinned local Splink dependencies are unavailable")
    return {
        "canary": canary,
        "evaluation": evaluation,
        "threshold": threshold,
    }


def _create_queue_run(canary_name: str) -> dict[str, str]:
    prerequisites = _queue_prerequisites(canary_name)
    canary = prerequisites["canary"]
    existing = frappe.get_all(
        RUN_DOCTYPE,
        filters={"canary_run": canary.name, "status": ["in", list(RUNNING_STATUSES) + ["Ready"]]},
        pluck="name",
        limit=1,
    )
    if existing:
        frappe.throw(f"Splink Review queue {existing[0]} already exists for this canary")
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
        "source_pair",
        "blocking_routes",
        "model_tier",
        "probabilistic_score",
        "review_threshold",
        "priority_rank",
        "review_status",
    ]
    now = frappe.utils.now_datetime()
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
        canary = frappe.get_doc(CANARY_DOCTYPE, run.canary_run)
        sources = policy.sources()
        placeholders = ", ".join(["%s"] * len(sources))
        raw_rows = frappe.db.sql(
            f"""SELECT * FROM `tabCCD Master`
                 WHERE modified <= %s AND ccd_reg_source IN ({placeholders})""",
            (run.snapshot_at, *sources),
            as_dict=True,
        )
        records = [_canonical_record(dict(row), policy) for row in raw_rows]
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
        if len(blocked.pairs) != int(canary.candidate_count or 0):
            frappe.throw(
                "The regenerated candidates differ from the frozen canary"
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
        training_records = _bounded_probability_records(records, training_ids)
        run.db_set(
            "training_record_count", len(training_records), update_modified=False
        )
        _set_status(run, "Training and Scoring Splink")
        predictions = score_requested_pairs(
            training_records,
            records,
            requested,
            minimum_probability=float(run.review_threshold),
            max_block_size=policy.max_block_size,
            max_prediction_pairs=policy.max_candidate_pairs,
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
    run = frappe.get_doc(RUN_DOCTYPE, candidate.queue_run)
    policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
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
        frappe.db.set_value(
            CANDIDATE_DOCTYPE,
            candidate.name,
            {"stale": 1, "review_status": "Stale"},
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
        "review_status": "Stale" if stale else candidate.review_status,
        "final_label": candidate.final_label or "",
        "priority_rank": candidate.priority_rank,
        "can_submit": bool(
            not stale
            and candidate.review_status in OPEN_REVIEW_STATUSES
            and not submitted
        ),
        "can_adjudicate": bool(
            "System Manager" in set(frappe.get_roles())
            and not stale
            and candidate.review_status == "Needs Adjudication"
        ),
    }
    if sensitive:
        payload["left"]["record_id"] = candidate.left_record
        payload["right"]["record_id"] = candidate.right_record
    if "System Manager" in set(frappe.get_roles()):
        payload["probabilistic_score"] = candidate.probabilistic_score
        payload["review_threshold"] = candidate.review_threshold
        payload["blocking_routes"] = candidate.blocking_routes
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
    _refresh_review_counts(candidate.queue_run)
    frappe.db.commit()
    return {"candidate": candidate.name, "status": candidate.review_status}


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
    _refresh_review_counts(candidate.queue_run)
    frappe.db.commit()
    return {
        "candidate": candidate.name,
        "status": candidate.review_status,
        "final_label": candidate.final_label or "",
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
