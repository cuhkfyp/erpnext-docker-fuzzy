"""Frappe APIs for the shadow CCD matching evaluation pilot.

This module never writes to ``CCD Master.match_table`` or sets ``is_matched``.
It stores predictions and human labels in dedicated evaluation DocTypes.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import replace
from typing import Any

import frappe

from db_connector.fuzzy_matching import normalization as norm
from db_connector.fuzzy_matching.blocking import generate_candidate_pairs
from db_connector.fuzzy_matching.clusters import inconsistent_pairs
from db_connector.fuzzy_matching.metrics import binary_metrics, cohens_kappa, select_thresholds
from db_connector.fuzzy_matching.models import compare_all_models
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.profiling import profile_attributes
from db_connector.fuzzy_matching.sampling import double_review_ids, stratified_sample
from db_connector.fuzzy_matching.security import mask_identifier
from db_connector.fuzzy_matching.splink_adapter import (
    SplinkUnavailable,
    available,
    dependency_versions,
    fit_predict,
)
from db_connector.fuzzy_matching.types import MatchTier, ModelResult

REVIEW_ROLE = "CCD Match Reviewer"
SENSITIVE_ROLE = "CCD Match Sensitive Reviewer"
ALLOWED_LABELS = {"Same", "Different", "Unsure"}
SENSITIVE_ATTRIBUTES = {"hkid", "hksr_num"}


def _require_reviewer() -> None:
    roles = set(frappe.get_roles())
    if "System Manager" not in roles and REVIEW_ROLE not in roles and SENSITIVE_ROLE not in roles:
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _policy_from_doc(doc: Any) -> MatchingPolicy:
    profiles: dict[str, dict[str, Any]] = {}
    for row in doc.get("source_profiles") or []:
        source = str(row.ccd_registration)
        item = profiles.setdefault(
            source,
            {"source": source, "field_map": {}, "identifier_scope": {}, "disabled_attributes": []},
        )
        attribute = str(row.canonical_attribute)
        if row.enabled:
            item["field_map"][attribute] = str(row.fieldname)
            if row.identifier_scope:
                item["identifier_scope"][attribute] = str(row.identifier_scope).lower()
        else:
            item["disabled_attributes"].append(attribute)
    trusted = [item.strip() for item in (doc.trusted_global_identifiers or "").split(",") if item.strip()]
    return MatchingPolicy.from_dict(
        {
            "name": doc.name,
            "version": doc.policy_version,
            "source_profiles": list(profiles.values()),
            "trusted_global_identifiers": trusted,
            "high_precision_target": doc.high_precision_target or 0.95,
            "minimum_high_samples": doc.minimum_high_samples or 30,
            "max_block_size": doc.max_block_size or 10_000,
            "max_candidate_pairs": doc.max_candidate_pairs or 500_000,
        }
    )


def _policy_snapshot(policy: MatchingPolicy) -> dict[str, Any]:
    return {
        "name": policy.name,
        "version": policy.version,
        "aliases": {key: list(value) for key, value in policy.aliases.items()},
        "source_profiles": [
            {
                "source": profile.source,
                "field_map": profile.field_map,
                "identifier_scope": profile.identifier_scope,
                "disabled_attributes": sorted(profile.disabled_attributes),
            }
            for profile in policy.source_profiles.values()
        ],
        "trusted_global_identifiers": sorted(policy.trusted_global_identifiers),
        "high_precision_target": policy.high_precision_target,
        "minimum_high_samples": policy.minimum_high_samples,
        "max_block_size": policy.max_block_size,
        "max_candidate_pairs": policy.max_candidate_pairs,
    }


def _canonical_record(row: dict[str, Any], policy: MatchingPolicy) -> dict[str, Any]:
    source = str(row.get("ccd_reg_source") or "")
    record = {
        "record_id": str(row.get("name") or ""),
        "source": source,
        "source_modified": row.get("modified"),
        "ccd_source_key": row.get("ccd_source_key"),
    }
    for attribute in policy.attributes():
        record[attribute] = policy.value(row, attribute)
    record["chi_full"] = norm.chinese_compact(
        f"{record.get('chi_surname') or ''}{record.get('chi_firstname') or ''}"
    )
    record["eng_full"] = norm.english_words(
        f"{record.get('eng_surname') or ''} {record.get('eng_firstname') or ''}"
    )
    record["eng_surname"] = norm.english_words(record.get("eng_surname"))
    record["eng_given_prefix"] = norm.english_compact(record.get("eng_firstname"))[:2]
    record["surname_key"] = norm.chinese_compact(record.get("chi_surname")) or norm.english_compact(
        record.get("eng_surname")
    )
    record["birthday"] = norm.birthday(record.get("birthday"))
    record["phone"] = norm.phone(record.get("phone"))
    record["email"] = norm.email(record.get("email"))
    global_values = []
    for attribute in policy.trusted_global_identifiers:
        if policy.globally_comparable(source, attribute):
            value = norm.identifier(record.get(attribute))
            if value:
                global_values.append(f"{attribute}:{value}")
    record["global_id"] = "|".join(sorted(global_values))
    return record


def _probability_map(
    records: list[dict[str, Any]], policy: MatchingPolicy
) -> tuple[dict[tuple[str, str], float], str | None]:
    if not available():
        return {}, "splink_dependency_unavailable"
    try:
        predictions = fit_predict(
            records,
            max_block_size=policy.max_block_size,
            max_prediction_pairs=policy.max_candidate_pairs,
        )
    except (SplinkUnavailable, ValueError) as exc:
        return {}, str(exc)
    except Exception as exc:
        # The statistical model is optional during a shadow run. Sparse data,
        # singular EM estimates, or backend incompatibilities must not prevent
        # the deterministic models and review sample from being generated.
        return {}, f"splink_training_failed:{type(exc).__name__}"
    output = {}
    for prediction in predictions:
        key = tuple(sorted((prediction.left_id, prediction.right_id)))
        output[key] = prediction.probability
    return output, None


def _formula_baseline(
    result: Any,
    left_raw: dict[str, Any],
    right_raw: dict[str, Any],
    left_formula: str,
    right_formula: str,
) -> Any:
    directional = []
    if left_formula:
        directional.append(("left_source_formula", left_formula, left_raw, right_raw))
    if right_formula:
        directional.append(("right_source_formula", right_formula, right_raw, left_raw))
    if not directional:
        return result

    try:
        from db_connector.api_ccd_fuzzy import evaluate_fuzzy_formula
    except Exception:
        return result

    scores = []
    for direction, formula, source, candidate in directional:
        try:
            score, is_match = evaluate_fuzzy_formula(formula, source, candidate)
            scores.append((float(score), bool(is_match), direction))
        except Exception:
            continue
    if not scores:
        return result

    score, _, selected_direction = max(scores, key=lambda item: item[0])
    is_match = any(item[1] for item in scores)
    baseline = ModelResult(
        "current_weighted_formula",
        score,
        MatchTier.REVIEW if is_match else MatchTier.LOW,
        ("registration_fuzzymachingscript", selected_direction),
        result.baseline.evidence,
    )
    return replace(result, baseline=baseline)


@frappe.whitelist()
def ensure_matching_roles() -> dict[str, str]:
    _require_manager()
    for role_name in (REVIEW_ROLE, SENSITIVE_ROLE):
        if not frappe.db.exists("Role", role_name):
            frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1}).insert(
                ignore_permissions=True
            )
    frappe.db.commit()
    return {"reviewer": REVIEW_ROLE, "sensitive_reviewer": SENSITIVE_ROLE}


@frappe.whitelist()
def enqueue_evaluation(
    policy_name: str,
    sample_size: int = 500,
    double_review_count: int = 100,
) -> dict[str, str]:
    _require_manager()
    policy_doc = frappe.get_doc("CCD Matching Policy", policy_name)
    if policy_doc.status not in {"Draft", "Pilot"}:
        frappe.throw("Only Draft or Pilot policies may create shadow runs")
    sample_size = max(1, min(int(sample_size), 5_000))
    double_review_count = max(0, min(int(double_review_count), sample_size))
    policy = _policy_from_doc(policy_doc)
    run = frappe.get_doc(
        {
            "doctype": "CCD Match Evaluation Run",
            "matching_policy": policy_name,
            "policy_version": policy_doc.policy_version,
            "policy_snapshot_json": _json(_policy_snapshot(policy)),
            "status": "Queued",
            "snapshot_at": frappe.utils.now_datetime(),
            "sample_size": sample_size,
            "double_review_count": double_review_count,
            "approval_status": "Pending Management Review",
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.enqueue(
        "db_connector.api_fuzzy_evaluation.run_evaluation",
        queue="long",
        timeout=14_400,
        enqueue_after_commit=True,
        run_name=run.name,
    )
    return {"run": run.name, "status": "Queued"}


def run_evaluation(run_name: str) -> None:
    run = frappe.get_doc("CCD Match Evaluation Run", run_name)
    run.db_set("status", "Profiling")
    try:
        policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
        raw_rows = frappe.db.sql(
            "SELECT * FROM `tabCCD Master` WHERE modified <= %s",
            (run.snapshot_at,),
            as_dict=True,
        )
        records = [_canonical_record(dict(row), policy) for row in raw_rows]
        raw_by_id = {str(row.name): dict(row) for row in raw_rows}
        record_by_id = {row["record_id"]: row for row in records if row["record_id"]}
        run.db_set("record_count", len(records), update_modified=False)
        run.db_set("profile_json", _json(profile_attributes(raw_rows, policy)), update_modified=False)

        run.db_set("status", "Generating Candidates")
        blocked = generate_candidate_pairs(records, policy)
        run.db_set("candidate_count", len(blocked.pairs), update_modified=False)
        run.db_set("candidate_truncated", int(blocked.truncated), update_modified=False)
        run.db_set("skipped_blocks_json", _json(blocked.skipped_blocks), update_modified=False)

        run.db_set("status", "Scoring")
        probabilities, probability_warning = _probability_map(records, policy)
        formulas = {
            item.name: str(item.fuzzymachingscript or "")
            for item in frappe.get_all("CCD Registration", fields=["name", "fuzzymachingscript"])
        }
        results = []
        for pair in blocked.pairs:
            left = record_by_id[pair.left_id]
            right = record_by_id[pair.right_id]
            probability = probabilities.get(tuple(sorted((pair.left_id, pair.right_id))))
            result = compare_all_models(pair, left, right, policy, probability=probability)
            result = _formula_baseline(
                result,
                raw_by_id[pair.left_id],
                raw_by_id[pair.right_id],
                formulas.get(left["source"], ""),
                formulas.get(right["source"], ""),
            )
            results.append(result)

        cluster_conflicts = inconsistent_pairs(results)
        sampled = stratified_sample(results, int(run.sample_size), seed=run.name)
        doubles = double_review_ids(sampled, int(run.double_review_count), seed=run.name)
        for result in sampled:
            pair_key = f"{result.pair.left_id}::{result.pair.right_id}"
            doc = frappe.get_doc(
                {
                    "doctype": "CCD Match Evaluation Pair",
                    "evaluation_run": run.name,
                    "left_record": result.pair.left_id,
                    "right_record": result.pair.right_id,
                    "left_source": record_by_id[result.pair.left_id]["source"],
                    "right_source": record_by_id[result.pair.right_id]["source"],
                    "left_modified_at": record_by_id[result.pair.left_id]["source_modified"],
                    "right_modified_at": record_by_id[result.pair.right_id]["source_modified"],
                    "source_pair": result.pair.source_pair,
                    "blocking_routes": ", ".join(result.pair.blocking_routes),
                    "baseline_score": result.baseline.score,
                    "baseline_tier": result.baseline.tier.value,
                    "tiered_score": result.tiered_gated.score,
                    "tiered_tier": result.tiered_gated.tier.value,
                    "recoverable_tier": result.tiered_recoverable.tier.value,
                    "probabilistic_score": (
                        result.probabilistic.probability if result.probabilistic else None
                    ),
                    "hybrid_tier": (
                        "pending_calibration"
                        if result.probabilistic
                        else result.tiered_gated.tier.value
                    ),
                    "reason_codes_json": _json(
                        {
                            "baseline": result.baseline.reasons,
                            "tiered_gated": result.tiered_gated.reasons,
                            "tiered_recoverable": result.tiered_recoverable.reasons,
                            "hybrid": result.hybrid.reasons if result.hybrid else (),
                        }
                    ),
                    "needs_double_review": int(pair_key in doubles),
                    "review_status": "Unreviewed",
                    "cluster_conflict": int(
                        tuple(sorted((result.pair.left_id, result.pair.right_id))) in cluster_conflicts
                    ),
                }
            )
            doc.insert(ignore_permissions=True)

        run.db_set("sampled_pair_count", len(sampled), update_modified=False)
        run.db_set(
            "model_versions_json",
            _json(
                {
                    "baseline": "registration_fuzzymachingscript",
                    "baseline_formula_sha256": {
                        source: hashlib.sha256(formula.encode()).hexdigest()
                        for source, formula in sorted(formulas.items())
                    },
                    "tiered": policy.version,
                    "splink": dependency_versions(),
                    "splink_status": "local" if not probability_warning else "unavailable",
                    "splink_warning": probability_warning,
                }
            ),
            update_modified=False,
        )
        run.db_set("status", "Reviewing", update_modified=False)
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        safe_message = f"{type(exc).__name__}: evaluation failed; inspect protected Error Log"
        run = frappe.get_doc("CCD Match Evaluation Run", run_name)
        run.db_set("status", "Failed", update_modified=False)
        run.db_set("error_summary", safe_message, update_modified=False)
        frappe.log_error(traceback.format_exc(), "CCD Match Evaluation Failure")
        frappe.db.commit()


@frappe.whitelist()
def submit_review(pair_name: str, label: str, notes: str = "") -> dict[str, str]:
    _require_reviewer()
    if label not in ALLOWED_LABELS:
        frappe.throw("Label must be Same, Different, or Unsure")
    pair = frappe.get_doc("CCD Match Evaluation Pair", pair_name)
    if _pair_is_stale(pair):
        pair.db_set("stale", 1, update_modified=False)
        frappe.throw("This pair is stale. Create a new evaluation run before reviewing it.")
    current_user = frappe.session.user
    existing = next(
        (row for row in pair.review_labels if row.reviewer == current_user and not row.is_adjudication),
        None,
    )
    if existing:
        existing.label = label
        existing.notes = notes
        existing.submitted_at = frappe.utils.now_datetime()
    else:
        pair.append(
            "review_labels",
            {
                "reviewer": current_user,
                "label": label,
                "notes": notes,
                "submitted_at": frappe.utils.now_datetime(),
                "is_adjudication": 0,
            },
        )
    ordinary = [row.label for row in pair.review_labels if not row.is_adjudication]
    required = 2 if pair.needs_double_review else 1
    if "Unsure" in ordinary:
        pair.review_status = "Needs Adjudication"
    elif len(ordinary) < required:
        pair.review_status = "Partially Reviewed"
    elif len(set(ordinary)) == 1:
        pair.review_status = "Agreed"
        pair.final_label = ordinary[0]
    else:
        pair.review_status = "Needs Adjudication"
    pair.save(ignore_permissions=True)
    frappe.db.commit()
    return {"pair": pair.name, "status": pair.review_status}


@frappe.whitelist()
def adjudicate_review(pair_name: str, label: str, notes: str = "") -> dict[str, str]:
    _require_manager()
    if label not in {"Same", "Different"}:
        frappe.throw("Adjudication must be Same or Different")
    pair = frappe.get_doc("CCD Match Evaluation Pair", pair_name)
    if _pair_is_stale(pair):
        pair.db_set("stale", 1, update_modified=False)
        frappe.throw("This pair is stale. Create a new evaluation run before adjudicating it.")
    if pair.review_status != "Needs Adjudication":
        frappe.throw("Only pairs awaiting adjudication may be adjudicated")
    pair.append(
        "review_labels",
        {
            "reviewer": frappe.session.user,
            "label": label,
            "notes": notes,
            "submitted_at": frappe.utils.now_datetime(),
            "is_adjudication": 1,
        },
    )
    pair.final_label = label
    pair.review_status = "Adjudicated"
    pair.save(ignore_permissions=True)
    frappe.db.commit()
    return {"pair": pair.name, "status": pair.review_status, "final_label": label}


def _stable_partition(name: str) -> str:
    return "calibration" if int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 5 < 3 else "held_out"


def _pair_is_stale(pair: Any) -> bool:
    left_modified = frappe.db.get_value("CCD Master", pair.left_record, "modified")
    right_modified = frappe.db.get_value("CCD Master", pair.right_record, "modified")
    return (
        not left_modified
        or not right_modified
        or frappe.utils.get_datetime(left_modified) != frappe.utils.get_datetime(pair.left_modified_at)
        or frappe.utils.get_datetime(right_modified) != frappe.utils.get_datetime(pair.right_modified_at)
    )


def _metrics_dict(value: Any) -> dict[str, Any] | None:
    return value.__dict__ if value else None


def _calibrate_scores(
    pairs: list[Any],
    score_field: str,
    policy: MatchingPolicy,
) -> dict[str, Any]:
    calibration: list[tuple[bool, float]] = []
    held_out: list[tuple[bool, float]] = []
    for pair in pairs:
        score = pair.get(score_field)
        if score is None:
            continue
        target = calibration if _stable_partition(pair.name) == "calibration" else held_out
        target.append((pair.final_label == "Same", float(score)))
    selection = select_thresholds(
        calibration,
        high_precision_target=policy.high_precision_target,
        minimum_high_samples=policy.minimum_high_samples,
    )
    held_high = (
        binary_metrics(held_out, selection.high_threshold)
        if held_out and selection.high_threshold is not None
        else None
    )
    high_threshold = selection.high_threshold
    if not held_high or (
        held_high.precision < policy.high_precision_target
        or held_high.true_positive + held_high.false_positive < policy.minimum_high_samples
    ):
        high_threshold = None
    held_review = (
        binary_metrics(held_out, selection.review_threshold)
        if held_out and selection.review_threshold is not None
        else None
    )
    return {
        "available_pairs": len(calibration) + len(held_out),
        "calibration_pairs": len(calibration),
        "held_out_pairs": len(held_out),
        "high_threshold": high_threshold,
        "review_threshold": selection.review_threshold,
        "calibration_high": _metrics_dict(selection.high_metrics),
        "calibration_review": _metrics_dict(selection.review_metrics),
        "held_out_high": _metrics_dict(held_high),
        "held_out_review": _metrics_dict(held_review),
        "warning": selection.warning if high_threshold is not None else "high_tier_disabled",
    }


def _fixed_tier_metrics(
    pairs: list[Any],
    tier_field: str,
    positive_tiers: set[str],
) -> dict[str, Any]:
    all_rows = []
    held_out = []
    for pair in pairs:
        row = (pair.final_label == "Same", 1.0 if pair.get(tier_field) in positive_tiers else 0.0)
        all_rows.append(row)
        if _stable_partition(pair.name) == "held_out":
            held_out.append(row)
    return {
        "all_labeled": _metrics_dict(binary_metrics(all_rows, 0.5)),
        "held_out": _metrics_dict(binary_metrics(held_out, 0.5)) if held_out else None,
    }


def _hybrid_tier(
    tiered_tier: str,
    probability: float | None,
    high_threshold: float | None,
    review_threshold: float | None,
) -> str:
    if probability is None:
        return tiered_tier
    if tiered_tier == MatchTier.CONFLICT.value:
        return MatchTier.CONFLICT.value
    if (
        high_threshold is not None
        and probability >= high_threshold
        and tiered_tier != MatchTier.LOW.value
    ):
        return MatchTier.HIGH.value
    if review_threshold is not None and probability >= review_threshold:
        return MatchTier.REVIEW.value
    return MatchTier.LOW.value


@frappe.whitelist()
def finalize_evaluation(run_name: str) -> dict[str, Any]:
    _require_manager()
    run = frappe.get_doc("CCD Match Evaluation Run", run_name)
    if run.status not in {"Reviewing", "Awaiting Management Approval"}:
        frappe.throw("Only a run in Reviewing status can be finalized")
    policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
    all_pairs = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={"evaluation_run": run.name},
        fields=[
            "name",
            "final_label",
            "stale",
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
            "baseline_score",
            "baseline_tier",
            "tiered_tier",
            "recoverable_tier",
            "probabilistic_score",
        ],
    )
    pairs = []
    for pair in all_pairs:
        if pair.stale or _pair_is_stale(pair):
            if not pair.stale:
                frappe.db.set_value(
                    "CCD Match Evaluation Pair", pair.name, "stale", 1, update_modified=False
                )
            continue
        pairs.append(pair)
    unresolved = [pair.name for pair in pairs if pair.final_label not in {"Same", "Different"}]
    if unresolved:
        frappe.throw(f"{len(unresolved)} non-stale pairs still require review or adjudication")
    if not pairs:
        frappe.throw("No adjudicated, non-stale pairs are available for finalization")

    baseline_calibration = _calibrate_scores(pairs, "baseline_score", policy)
    probabilistic_calibration = _calibrate_scores(pairs, "probabilistic_score", policy)
    high_threshold = probabilistic_calibration["high_threshold"]
    review_threshold = probabilistic_calibration["review_threshold"]
    for pair in pairs:
        pair.hybrid_tier = _hybrid_tier(
            pair.tiered_tier,
            pair.probabilistic_score,
            high_threshold,
            review_threshold,
        )
        frappe.db.set_value(
            "CCD Match Evaluation Pair",
            pair.name,
            "hybrid_tier",
            pair.hybrid_tier,
            update_modified=False,
        )

    double_labels = []
    double_pairs = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={"evaluation_run": run.name, "needs_double_review": 1},
        pluck="name",
    )
    for pair_name in double_pairs:
        labels = frappe.get_all(
            "CCD Match Review Label",
            filters={"parent": pair_name, "is_adjudication": 0},
            pluck="label",
            order_by="idx asc",
        )
        if len(labels) >= 2:
            double_labels.append((labels[0], labels[1]))

    metrics = {
        "labeled_pairs": len(pairs),
        "reviewer_kappa": cohens_kappa(double_labels),
        "models": {
            "baseline_score_calibration": baseline_calibration,
            "baseline_current_flag": _fixed_tier_metrics(pairs, "baseline_tier", {MatchTier.REVIEW.value}),
            "tiered_gated_high": _fixed_tier_metrics(pairs, "tiered_tier", {MatchTier.HIGH.value}),
            "tiered_gated_review_queue": _fixed_tier_metrics(
                pairs, "tiered_tier", {MatchTier.HIGH.value, MatchTier.REVIEW.value}
            ),
            "tiered_recoverable_high": _fixed_tier_metrics(
                pairs, "recoverable_tier", {MatchTier.HIGH.value}
            ),
            "fellegi_sunter_calibration": probabilistic_calibration,
            "hybrid_high": _fixed_tier_metrics(pairs, "hybrid_tier", {MatchTier.HIGH.value}),
            "hybrid_review_queue": _fixed_tier_metrics(
                pairs, "hybrid_tier", {MatchTier.HIGH.value, MatchTier.REVIEW.value}
            ),
        },
    }
    run.db_set("metrics_json", _json(metrics), update_modified=False)
    run.db_set("status", "Awaiting Management Approval", update_modified=False)
    run.db_set("approval_status", "Pending Management Review", update_modified=False)
    frappe.db.commit()
    return metrics


@frappe.whitelist()
def set_evaluation_approval(run_name: str, decision: str) -> dict[str, str]:
    _require_manager()
    if decision not in {"Approved", "Rejected"}:
        frappe.throw("Decision must be Approved or Rejected")
    run = frappe.get_doc("CCD Match Evaluation Run", run_name)
    if run.status != "Awaiting Management Approval":
        frappe.throw("Finalize the evaluation before recording a management decision")
    run.db_set("approval_status", decision, update_modified=False)
    run.db_set("status", "Completed", update_modified=False)
    frappe.db.commit()
    return {"run": run.name, "status": "Completed", "approval_status": decision}


@frappe.whitelist()
def get_pair_evidence(pair_name: str) -> dict[str, Any]:
    _require_reviewer()
    pair = frappe.get_doc("CCD Match Evaluation Pair", pair_name)
    left = frappe.get_doc("CCD Master", pair.left_record)
    right = frappe.get_doc("CCD Master", pair.right_record)
    sensitive = "System Manager" in frappe.get_roles() or SENSITIVE_ROLE in frappe.get_roles()
    policy = MatchingPolicy.from_dict(
        json.loads(
            frappe.db.get_value(
                "CCD Match Evaluation Run", pair.evaluation_run, "policy_snapshot_json"
            )
        )
    )
    attributes = {}
    for attribute in policy.attributes():
        left_value = policy.value(left.as_dict(), attribute)
        right_value = policy.value(right.as_dict(), attribute)
        if (
            attribute in policy.trusted_global_identifiers or attribute in SENSITIVE_ATTRIBUTES
        ) and not sensitive:
            left_value = mask_identifier(left_value)
            right_value = mask_identifier(right_value)
        attributes[attribute] = {"left": left_value, "right": right_value}
    stale = _pair_is_stale(pair)
    if stale and not pair.stale:
        frappe.db.set_value("CCD Match Evaluation Pair", pair.name, "stale", 1, update_modified=False)
    payload = {
        "pair": pair.name,
        "stale": stale,
        "attributes": attributes,
    }
    if sensitive:
        payload["left_record"] = pair.left_record
        payload["right_record"] = pair.right_record
    if "System Manager" in frappe.get_roles():
        payload["reason_codes"] = json.loads(pair.reason_codes_json or "{}")
    return payload
