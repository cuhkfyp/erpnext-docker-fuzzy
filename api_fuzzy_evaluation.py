"""Frappe APIs for the shadow CCD matching evaluation pilot.

This module never writes to ``CCD Master.match_table`` or sets ``is_matched``.
It stores predictions and human labels in dedicated evaluation DocTypes.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import traceback
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import frappe

from db_connector.fuzzy_matching import normalization as norm
from db_connector.fuzzy_matching.blocking import BLOCKING_VERSION, generate_candidate_pairs
from db_connector.fuzzy_matching.clusters import inconsistent_pairs
from db_connector.fuzzy_matching.metrics import (
    binary_metrics,
    cohens_kappa,
    select_thresholds,
    wilson_interval,
)
from db_connector.fuzzy_matching.models import compare_all_models
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.profiling import profile_attributes
from db_connector.fuzzy_matching.sampling import (
    balanced_quotas,
    double_review_ids,
    stratified_sample,
)
from db_connector.fuzzy_matching.security import mask_identifier
from db_connector.fuzzy_matching.splink_adapter import (
    MAX_DIRECT_SCORING_PAIRS,
    RANDOM_MATCH_PRIOR,
    SPLINK_ADAPTER_VERSION,
    SplinkUnavailable,
    available,
    dependency_versions,
    fit_predict,
)
from db_connector.fuzzy_matching.types import (
    CandidatePair,
    EvaluationResult,
    MatchTier,
    ModelResult,
)

REVIEW_ROLE = "CCD Match Reviewer"
SENSITIVE_ROLE = "CCD Match Sensitive Reviewer"
ALLOWED_LABELS = {"Same", "Different", "Unsure"}
SENSITIVE_ATTRIBUTES = {"hkid", "hksr_num"}
MAX_SPLINK_TRAINING_RECORDS = 5_000
THRESHOLD_EVALUATION = "Threshold Evaluation"
POSITIVE_BENCHMARK = "Positive Benchmark"
HIGH_TIER_VALIDATION = "High Tier Validation"
DEFAULT_PILOT_POLICY_VERSION = "pilot-1.6"
LEGACY_BENCHMARK_MIN_SCORE = 0.9
POSITIVE_CONFIRMATION_REQUIRED = "Positive Confirmation Required"

IDENTITY_ATTRIBUTES = (
    "chi_surname",
    "chi_firstname",
    "eng_surname",
    "eng_firstname",
    "phone",
    "email",
    "birthday",
    "hksr_num",
    "hkid",
)

# CCD Registration maps arbitrary source columns into CCD Master fields. Only
# these targets are identity evidence; addresses, sex, application data, and
# other operational fields must never enter scoring merely because they exist.
FIELD_TO_IDENTITY_ATTRIBUTE = {
    "chi_surname": ("chi_surname", "Chinese Name", 0),
    "chi_firstname": ("chi_firstname", "Chinese Name", 0),
    "eng_surname": ("eng_surname", "English Name", 0),
    "eng_firstname": ("eng_firstname", "English Name", 0),
    "phone_num": ("phone", "Phone Exact", 0),
    "mobile": ("phone", "Phone Exact", 1),
    "res_phone": ("phone", "Phone Exact", 2),
    "email": ("email", "Email Exact", 0),
    "birthday": ("birthday", "Birthday Exact", 0),
    "hksr_num": ("hksr_num", "Identifier Exact", 0),
    "hkid": ("hkid", "Identifier Exact", 0),
}

DEFAULT_FIELDS = {
    "chi_surname": "chi_surname",
    "chi_firstname": "chi_firstname",
    "eng_surname": "eng_surname",
    "eng_firstname": "eng_firstname",
    "phone": "phone_num",
    "email": "email",
    "birthday": "birthday",
    "hksr_num": "hksr_num",
    "hkid": "hkid",
}


def _require_reviewer() -> None:
    roles = set(frappe.get_roles())
    if "System Manager" not in roles and REVIEW_ROLE not in roles and SENSITIVE_ROLE not in roles:
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _ordered_pair_key(left_id: Any, right_id: Any) -> tuple[str, str]:
    """Return an orientation-independent record-pair key."""
    return tuple(sorted((str(left_id), str(right_id))))


def _historical_evaluation_pair_keys(run_name: str) -> set[tuple[str, str]]:
    """Return pairs from prior valid or human-used evaluation sets.

    Threshold evaluations must remain genuinely held out.  Excluding all prior
    usable evaluation pairs also prevents reviewers from being asked to label
    the same records again, regardless of whether the older run was approved or
    rejected.  A Failed run with no labels is only a discarded quality-control
    artifact; retaining it here could exhaust a sparse source pair even though
    no human ever reviewed its sample.
    """
    rows = frappe.db.sql(
        """SELECT pair.left_record, pair.right_record
           FROM `tabCCD Match Evaluation Pair` pair
           INNER JOIN `tabCCD Match Evaluation Run` run
                   ON run.name = pair.evaluation_run
           WHERE pair.evaluation_run != %s
             AND (
                 run.status != 'Failed'
                 OR pair.evaluation_run IN (
                     SELECT reviewed_pair.evaluation_run
                     FROM `tabCCD Match Evaluation Pair` reviewed_pair
                     LEFT JOIN `tabCCD Match Review Label` review_label
                            ON review_label.parent = reviewed_pair.name
                     WHERE COALESCE(reviewed_pair.final_label, '') != ''
                        OR review_label.name IS NOT NULL
                     GROUP BY reviewed_pair.evaluation_run
                 )
             )""",
        run_name,
        as_dict=True,
    )
    return {
        _ordered_pair_key(row.left_record, row.right_record)
        for row in rows
    }


def _eligible_source_pair_counts(
    pairs: list[CandidatePair],
    excluded_pair_keys: set[tuple[str, str]],
) -> tuple[Counter, int]:
    """Count eligible candidates by source pair and report exclusions."""
    counts: Counter = Counter()
    excluded_count = 0
    for pair in pairs:
        if _ordered_pair_key(pair.left_id, pair.right_id) in excluded_pair_keys:
            excluded_count += 1
            continue
        counts[pair.source_pair] += 1
    return counts, excluded_count


def _label_reviewers(review_labels: list[Any], label: str) -> set[str]:
    """Return distinct human identities supporting a label, including adjudicators."""
    return {
        str(row.reviewer)
        for row in review_labels
        if row.label == label and str(row.reviewer or "")
    }


def _positive_confirmation_complete(review_labels: list[Any]) -> bool:
    return len(_label_reviewers(review_labels, "Same")) >= 2


def _mark_positive_confirmation_required(pair: Any) -> None:
    """Require a second Same without erasing a randomized-review assignment."""
    pair.needs_double_review = 1
    if not pair.double_review_reason:
        pair.double_review_reason = "positive_confirmation"


def _fieldname_from_registration(value: Any) -> str:
    """Extract ``fieldname`` from ``fieldname: Label`` registration values."""
    return str(value or "").split(":", 1)[0].strip()


def _registration_profile_rows(registration: Any) -> list[dict[str, Any]]:
    selected: dict[str, tuple[int, str, str]] = {}
    for row in registration.get("fieldmatch") or []:
        fieldname = _fieldname_from_registration(row.sys_fieldname)
        definition = FIELD_TO_IDENTITY_ATTRIBUTE.get(fieldname)
        if not definition:
            continue
        attribute, comparator, priority = definition
        current = selected.get(attribute)
        if current is None or priority < current[0]:
            selected[attribute] = (priority, fieldname, comparator)

    output = []
    for attribute in IDENTITY_ATTRIBUTES:
        configured = selected.get(attribute)
        default_field = DEFAULT_FIELDS[attribute]
        output.append(
            {
                "ccd_registration": registration.name,
                "canonical_attribute": attribute,
                "fieldname": configured[1] if configured else default_field,
                "comparator": (
                    configured[2]
                    if configured
                    else FIELD_TO_IDENTITY_ATTRIBUTE[default_field][1]
                ),
                "identifier_scope": (
                    "Global" if attribute == "hkid" and configured is not None else "Unknown"
                ),
                "reliability_status": (
                    "Approved" if attribute == "hkid" and configured is not None else "Unverified"
                ),
                "enabled": int(configured is not None),
            }
        )
    return output


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
            "minimum_positive_labels_per_split": (
                doc.minimum_positive_labels_per_split or 10
            ),
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
        "minimum_positive_labels_per_split": policy.minimum_positive_labels_per_split,
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
            raw_value = record.get(attribute)
            if attribute == "hkid" and not norm.valid_hkid(raw_value):
                continue
            value = norm.identifier(raw_value)
            if value:
                global_values.append(f"{attribute}:{value}")
    record["global_id"] = "|".join(sorted(global_values))
    return record


def _bounded_probability_records(
    records: list[dict[str, Any]],
    required_record_ids: set[str] | None = None,
    *,
    limit: int = MAX_SPLINK_TRAINING_RECORDS,
) -> list[dict[str, Any]]:
    """Return a deterministic bounded background sample plus required rows."""
    required = {str(item) for item in (required_record_ids or set()) if item}
    by_id = {str(row.get("record_id") or ""): row for row in records if row.get("record_id")}
    selected = [by_id[item] for item in sorted(required) if item in by_id]
    remaining = max(0, int(limit) - len(selected))
    if remaining:
        background = (row for record_id, row in by_id.items() if record_id not in required)
        selected.extend(
            heapq.nsmallest(
                remaining,
                background,
                key=lambda row: hashlib.sha256(str(row["record_id"]).encode()).digest(),
            )
        )
    return selected


def _select_positive_benchmark_rows(
    rows: list[dict[str, Any]],
    sample_size: int,
    *,
    seed: str,
) -> list[dict[str, Any]]:
    """Select unseen legacy-discovered pairs without using their score as a model feature."""
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = tuple(sorted((str(row["left_id"]), str(row["right_id"]))))
        current = deduplicated.get(key)
        if current is None or float(row.get("legacy_score") or 0) > float(
            current.get("legacy_score") or 0
        ):
            item = dict(row)
            item["left_id"], item["right_id"] = key
            deduplicated[key] = item

    grouped: dict[str, list[tuple[bytes, dict[str, Any]]]] = {}
    for row in deduplicated.values():
        digest = hashlib.sha256(
            f"benchmark:{seed}:{row['left_id']}:{row['right_id']}".encode()
        ).digest()
        grouped.setdefault(str(row["source_pair"]), []).append((digest, row))
    quotas = balanced_quotas(
        {source_pair: len(items) for source_pair, items in grouped.items()},
        sample_size,
    )
    selected = []
    for source_pair, items in sorted(grouped.items()):
        selected.extend(
            row
            for _, row in sorted(items, key=lambda item: item[0])[
                : quotas.get(source_pair, 0)
            ]
        )
    return sorted(
        selected,
        key=lambda row: hashlib.sha256(
            f"benchmark-output:{seed}:{row['left_id']}:{row['right_id']}".encode()
        ).digest(),
    )


def _select_high_tier_validation_results(
    results: Iterable[EvaluationResult],
    sample_size: int,
    *,
    seed: str,
) -> tuple[list[EvaluationResult], dict[str, Any]]:
    """Uniformly sample unseen deterministic-High predictions with bounded memory."""
    reservoir: list[tuple[int, int, EvaluationResult]] = []
    source_pair_counts: Counter = Counter()
    blocking_route_counts: Counter = Counter()
    high_candidate_count = 0
    sequence = 0
    for result in results:
        if result.tiered_gated.tier != MatchTier.HIGH:
            continue
        high_candidate_count += 1
        source_pair_counts[result.pair.source_pair] += 1
        blocking_route_counts["+".join(sorted(result.pair.blocking_routes))] += 1
        digest = int(
            hashlib.sha256(
                f"high-validation:{seed}:{result.pair.left_id}:{result.pair.right_id}".encode()
            ).hexdigest(),
            16,
        )
        item = (-digest, sequence, result)
        sequence += 1
        if len(reservoir) < sample_size:
            heapq.heappush(reservoir, item)
        elif digest < -reservoir[0][0]:
            heapq.heapreplace(reservoir, item)

    sampled = [
        result
        for _, _, result in sorted(
            reservoir,
            key=lambda item: (-item[0], item[1]),
        )
    ]
    return sampled, {
        "eligible_high_candidates": high_candidate_count,
        "source_pair_counts": dict(sorted(source_pair_counts.items())),
        "blocking_route_counts": dict(sorted(blocking_route_counts.items())),
        "sampling_method": "uniform_deterministic_bottom_k",
    }


def _positive_benchmark_rows(
    policy: MatchingPolicy,
    snapshot_at: Any,
    sample_size: int,
    *,
    seed: str,
) -> list[dict[str, Any]]:
    sources = policy.sources()
    placeholders = ", ".join(["%s"] * len(sources))
    rows = frappe.db.sql(
        f"""SELECT l.name AS left_id, r.name AS right_id,
                   l.ccd_reg_source AS left_source,
                   r.ccd_reg_source AS right_source,
                   CONCAT(LEAST(l.ccd_reg_source, r.ccd_reg_source), '::',
                          GREATEST(l.ccd_reg_source, r.ccd_reg_source)) AS source_pair,
                   MAX(m.score) AS legacy_score
              FROM `tabCCD Master matching` m
              JOIN `tabCCD Master` l ON l.name = m.parent
              JOIN `tabCCD Master` r
                ON r.ccd_reg_source = m.client
               AND r.ccd_source_key = m.client_id
             WHERE l.modified <= %s AND r.modified <= %s
               AND m.score >= %s
               AND l.ccd_reg_source IN ({placeholders})
               AND r.ccd_reg_source IN ({placeholders})
               AND l.ccd_reg_source != r.ccd_reg_source
          GROUP BY l.name, r.name, l.ccd_reg_source, r.ccd_reg_source""",
        (snapshot_at, snapshot_at, LEGACY_BENCHMARK_MIN_SCORE, *sources, *sources),
        as_dict=True,
    )
    prior_pair_keys = {
        tuple(sorted((str(row.left_record), str(row.right_record))))
        for row in frappe.get_all(
            "CCD Match Evaluation Pair",
            fields=["left_record", "right_record"],
        )
    }
    unseen = [
        dict(row)
        for row in rows
        if tuple(sorted((str(row.left_id), str(row.right_id)))) not in prior_pair_keys
    ]
    return _select_positive_benchmark_rows(unseen, sample_size, seed=seed)


def _probability_map(
    records: list[dict[str, Any]],
    policy: MatchingPolicy,
    required_record_ids: set[str] | None = None,
    requested_pairs: set[tuple[str, str]] | None = None,
) -> tuple[dict[tuple[str, str], float], str | None, int]:
    training_records = _bounded_probability_records(records, required_record_ids)
    if not available():
        return {}, "splink_dependency_unavailable", len(training_records)
    try:
        predictions = fit_predict(
            training_records,
            max_block_size=policy.max_block_size,
            max_prediction_pairs=policy.max_candidate_pairs,
            requested_pairs=requested_pairs,
        )
    except (SplinkUnavailable, ValueError) as exc:
        return {}, str(exc), len(training_records)
    except Exception as exc:
        # The statistical model is optional during a shadow run. Sparse data,
        # singular EM estimates, or backend incompatibilities must not prevent
        # the deterministic models and review sample from being generated.
        return {}, f"splink_training_failed:{type(exc).__name__}", len(training_records)
    output = {}
    for prediction in predictions:
        key = tuple(sorted((prediction.left_id, prediction.right_id)))
        output[key] = prediction.probability
    return output, None, len(training_records)


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


def _ensure_matching_roles() -> dict[str, str]:
    for role_name in (REVIEW_ROLE, SENSITIVE_ROLE):
        if not frappe.db.exists("Role", role_name):
            frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1}).insert(
                ignore_permissions=True
            )
    frappe.db.commit()
    return {"reviewer": REVIEW_ROLE, "sensitive_reviewer": SENSITIVE_ROLE}


@frappe.whitelist()
def ensure_matching_roles() -> dict[str, str]:
    _require_manager()
    return _ensure_matching_roles()


def install_matching_roles() -> dict[str, str]:
    """Bench-only migration helper; deliberately not whitelisted."""
    return _ensure_matching_roles()


def _sync_policy_source_profiles(policy_name: str) -> dict[str, Any]:
    policy_doc = frappe.get_doc("CCD Matching Policy", policy_name)
    if policy_doc.status != "Draft":
        frappe.throw("Source mappings can only be synchronized on a Draft policy")

    record_sources = frappe.db.sql_list(
        """SELECT DISTINCT ccd_reg_source
           FROM `tabCCD Master`
           WHERE COALESCE(ccd_reg_source, '') != ''
           ORDER BY ccd_reg_source"""
    )
    policy_doc.set("source_profiles", [])
    imported_sources = []
    skipped_sources = []
    for source in record_sources:
        if not frappe.db.exists("CCD Registration", source):
            skipped_sources.append(source)
            continue
        registration = frappe.get_doc("CCD Registration", source)
        for row in _registration_profile_rows(registration):
            policy_doc.append("source_profiles", row)
        imported_sources.append(source)
    if not imported_sources:
        frappe.throw("No CCD Master sources have a matching CCD Registration")
    policy_doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "policy": policy_doc.name,
        "imported_sources": imported_sources,
        "skipped_sources": skipped_sources,
        "profile_rows": len(policy_doc.source_profiles),
    }


@frappe.whitelist()
def sync_policy_source_profiles(policy_name: str) -> dict[str, Any]:
    """Replace a Draft policy's profiles from live CCD Registration mappings."""
    _require_manager()
    return _sync_policy_source_profiles(policy_name)


def _ensure_default_pilot_policy(policy_version: str) -> dict[str, Any]:
    if frappe.db.exists("CCD Matching Policy", policy_version):
        return {"policy": policy_version, "created": False}
    frappe.get_doc(
        {
            "doctype": "CCD Matching Policy",
            "policy_version": policy_version,
            "title": f"CCD Recommendation-Only Matching Pilot {policy_version}",
            "status": "Draft",
            "trusted_global_identifiers": "hkid",
            "high_precision_target": 0.95,
            "minimum_high_samples": 30,
            "minimum_positive_labels_per_split": 10,
            "max_block_size": 10_000,
            "max_candidate_pairs": 1_000_000,
            "notes": (
                "Generated from CCD Registration mappings. Strong identifier scope "
                "starts Unknown and must be approved before it can create a High match. "
                "Only structurally complete, check-digit-valid HKIDs may act as "
                "global identifiers; partial, masked, and invalid values remain "
                "review-only evidence. Source-balanced sampling, positive "
                "confirmation, and split-level validation safeguards are enabled."
            ),
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    result = _sync_policy_source_profiles(policy_version)
    result["created"] = True
    return result


@frappe.whitelist()
def ensure_default_pilot_policy(
    policy_version: str = DEFAULT_PILOT_POLICY_VERSION,
) -> dict[str, Any]:
    """Create the initial safe policy and import centre mappings idempotently."""
    _require_manager()
    return _ensure_default_pilot_policy(policy_version)


def install_default_pilot_policy(
    policy_version: str = DEFAULT_PILOT_POLICY_VERSION,
) -> dict[str, Any]:
    """Bench-only deployment helper; deliberately not whitelisted."""
    return _ensure_default_pilot_policy(policy_version)


def _enqueue_evaluation(
    policy_name: str,
    sample_size: int = 500,
    double_review_count: int = 100,
    *,
    run_purpose: str = THRESHOLD_EVALUATION,
) -> dict[str, str]:
    policy_doc = frappe.get_doc("CCD Matching Policy", policy_name)
    if policy_doc.status not in {"Draft", "Pilot"}:
        frappe.throw("Only Draft or Pilot policies may create shadow runs")
    sample_size = max(1, min(int(sample_size), 5_000))
    double_review_count = max(0, min(int(double_review_count), sample_size))
    if run_purpose not in {
        THRESHOLD_EVALUATION,
        POSITIVE_BENCHMARK,
        HIGH_TIER_VALIDATION,
    }:
        frappe.throw("Unsupported evaluation run purpose")
    if run_purpose == HIGH_TIER_VALIDATION:
        double_review_count = sample_size
    policy = _policy_from_doc(policy_doc)
    if not policy.sources():
        frappe.throw("The policy has no source profiles; import CCD Registration mappings first")
    run = frappe.get_doc(
        {
            "doctype": "CCD Match Evaluation Run",
            "matching_policy": policy_name,
            "policy_version": policy_doc.policy_version,
            "run_purpose": run_purpose,
            "policy_snapshot_json": _json(_policy_snapshot(policy)),
            "status": "Queued",
            "snapshot_at": frappe.utils.now_datetime(),
            "sample_size": sample_size,
            "double_review_count": double_review_count,
            "approval_status": "Pending Management Review",
        }
    ).insert(ignore_permissions=True)
    frappe.enqueue(
        "db_connector.api_fuzzy_evaluation.run_evaluation",
        queue="long",
        timeout=14_400,
        enqueue_after_commit=True,
        run_name=run.name,
    )
    # Register the callback before committing so every caller, including a
    # standalone deployment helper, durably creates the run before the worker
    # can claim it and reliably fires the enqueue callback in this transaction.
    frappe.db.commit()
    return {"run": run.name, "status": "Queued"}


@frappe.whitelist()
def enqueue_evaluation(
    policy_name: str,
    sample_size: int = 500,
    double_review_count: int = 100,
) -> dict[str, str]:
    """Queue a shadow run from Desk after enforcing manager permissions."""
    _require_manager()
    return _enqueue_evaluation(policy_name, sample_size, double_review_count)


def install_evaluation_run(
    policy_name: str = DEFAULT_PILOT_POLICY_VERSION,
    sample_size: int = 500,
    double_review_count: int = 100,
) -> dict[str, str]:
    """Bench-only run launcher; deliberately not exposed as a web method.

    Deployment operators can start an evaluation without placing an ERPNext
    password in shell history.  The public API above remains manager-only.
    """
    return _enqueue_evaluation(policy_name, sample_size, double_review_count)


def install_positive_benchmark_run(
    policy_name: str = DEFAULT_PILOT_POLICY_VERSION,
    sample_size: int = 100,
    double_review_count: int = 20,
) -> dict[str, str]:
    """Bench-only launcher for an unseen, positive-enriched blocking benchmark."""
    return _enqueue_evaluation(
        policy_name,
        sample_size,
        double_review_count,
        run_purpose=POSITIVE_BENCHMARK,
    )


def install_high_tier_validation_run(
    policy_name: str = DEFAULT_PILOT_POLICY_VERSION,
    sample_size: int = 100,
) -> dict[str, str]:
    """Bench-only launcher for an unseen, fully double-reviewed High sample."""
    return _enqueue_evaluation(
        policy_name,
        sample_size,
        sample_size,
        run_purpose=HIGH_TIER_VALIDATION,
    )


def run_evaluation(run_name: str) -> None:
    run = frappe.get_doc("CCD Match Evaluation Run", run_name)
    run.db_set("status", "Profiling")
    try:
        policy = MatchingPolicy.from_dict(json.loads(run.policy_snapshot_json))
        sources = policy.sources()
        if len(sources) < 2:
            frappe.throw("At least two governed CCD sources are required for an evaluation")
        placeholders = ", ".join(["%s"] * len(sources))
        raw_rows = frappe.db.sql(
            f"""SELECT * FROM `tabCCD Master`
                WHERE modified <= %s AND ccd_reg_source IN ({placeholders})""",
            (run.snapshot_at, *sources),
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
        formulas = {
            item.name: str(item.fuzzymachingscript or "")
            for item in frappe.get_all(
                "CCD Registration",
                filters={"name": ["in", list(sources)]},
                fields=["name", "fuzzymachingscript"],
            )
        }
        historical_pair_keys: set[tuple[str, str]] = set()
        historical_candidate_exclusions = 0
        eligible_source_pair_counts = Counter(pair.source_pair for pair in blocked.pairs)
        if (run.run_purpose or THRESHOLD_EVALUATION) in {
            THRESHOLD_EVALUATION,
            HIGH_TIER_VALIDATION,
        }:
            historical_pair_keys = _historical_evaluation_pair_keys(run.name)
            eligible_source_pair_counts, historical_candidate_exclusions = (
                _eligible_source_pair_counts(blocked.pairs, historical_pair_keys)
            )

        def evaluated_results(*, apply_formula: bool = True):
            for pair in blocked.pairs:
                if _ordered_pair_key(pair.left_id, pair.right_id) in historical_pair_keys:
                    continue
                left = record_by_id[pair.left_id]
                right = record_by_id[pair.right_id]
                result = compare_all_models(pair, left, right, policy)
                if apply_formula:
                    result = _formula_baseline(
                        result,
                        raw_by_id[pair.left_id],
                        raw_by_id[pair.right_id],
                        formulas.get(left["source"], ""),
                        formulas.get(right["source"], ""),
                    )
                yield result

        benchmark_metadata: dict[tuple[str, str], dict[str, Any]] = {}
        high_validation_metadata: dict[str, Any] = {}
        if (run.run_purpose or THRESHOLD_EVALUATION) == POSITIVE_BENCHMARK:
            benchmark_rows = _positive_benchmark_rows(
                policy,
                run.snapshot_at,
                int(run.sample_size),
                seed=run.name,
            )
            if not benchmark_rows:
                frappe.throw("No unseen legacy-discovered pairs are available for benchmarking")
            benchmark_keys = {
                tuple(sorted((str(row["left_id"]), str(row["right_id"]))))
                for row in benchmark_rows
            }
            recovered = {}
            for pair in blocked.pairs:
                key = tuple(sorted((pair.left_id, pair.right_id)))
                if key in benchmark_keys:
                    recovered[key] = pair
            sampled = []
            for row in benchmark_rows:
                key = tuple(sorted((str(row["left_id"]), str(row["right_id"]))))
                candidate = recovered.get(key) or CandidatePair(
                    key[0],
                    key[1],
                    str(row["source_pair"]),
                    ("legacy_benchmark_unrecovered",),
                )
                left = record_by_id[candidate.left_id]
                right = record_by_id[candidate.right_id]
                sampled.append(
                    _formula_baseline(
                        compare_all_models(candidate, left, right, policy),
                        raw_by_id[candidate.left_id],
                        raw_by_id[candidate.right_id],
                        formulas.get(left["source"], ""),
                        formulas.get(right["source"], ""),
                    )
                )
                benchmark_metadata[key] = {
                    "benchmark_origin": "legacy_score_gte_090_discovery_only",
                    "candidate_recovered": int(key in recovered),
                }
        elif (run.run_purpose or THRESHOLD_EVALUATION) == HIGH_TIER_VALIDATION:
            sampled, high_validation_metadata = _select_high_tier_validation_results(
                evaluated_results(apply_formula=False),
                int(run.sample_size),
                seed=run.name,
            )
            if len(sampled) != int(run.sample_size):
                frappe.throw(
                    "Fewer unseen deterministic-High pairs are available than requested"
                )
            sampled = [
                _formula_baseline(
                    result,
                    raw_by_id[result.pair.left_id],
                    raw_by_id[result.pair.right_id],
                    formulas.get(record_by_id[result.pair.left_id]["source"], ""),
                    formulas.get(record_by_id[result.pair.right_id]["source"], ""),
                )
                for result in sampled
            ]
        else:
            sampled = stratified_sample(
                evaluated_results(),
                int(run.sample_size),
                seed=run.name,
                source_pair_counts=dict(eligible_source_pair_counts),
            )
        required_ids = {
            record_id
            for result in sampled
            for record_id in (result.pair.left_id, result.pair.right_id)
        }
        requested_pairs = {
            tuple(sorted((result.pair.left_id, result.pair.right_id)))
            for result in sampled
        }
        probabilities, probability_warning, splink_training_count = _probability_map(
            records,
            policy,
            required_ids,
            requested_pairs,
        )
        rescored = []
        for result in sampled:
            pair = result.pair
            left = record_by_id[pair.left_id]
            right = record_by_id[pair.right_id]
            probability = probabilities.get(tuple(sorted((pair.left_id, pair.right_id))))
            rescored.append(
                _formula_baseline(
                    compare_all_models(pair, left, right, policy, probability=probability),
                    raw_by_id[pair.left_id],
                    raw_by_id[pair.right_id],
                    formulas.get(left["source"], ""),
                    formulas.get(right["source"], ""),
                )
            )
        sampled = rescored
        cluster_conflicts = inconsistent_pairs(sampled)
        doubles = double_review_ids(sampled, int(run.double_review_count), seed=run.name)
        for result in sampled:
            pair_key = f"{result.pair.left_id}::{result.pair.right_id}"
            metadata = benchmark_metadata.get(
                tuple(sorted((result.pair.left_id, result.pair.right_id))),
                {},
            )
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
                    "benchmark_origin": metadata.get("benchmark_origin", ""),
                    "candidate_recovered": int(metadata.get("candidate_recovered", 0)),
                    "baseline_score": result.baseline.score,
                    "baseline_tier": result.baseline.tier.value,
                    "tiered_score": result.tiered_gated.score,
                    "tiered_tier": result.tiered_gated.tier.value,
                    "recoverable_tier": result.tiered_recoverable.tier.value,
                    "probabilistic_score": (
                        result.probabilistic.probability if result.probabilistic else 0
                    ),
                    "probabilistic_available": int(result.probabilistic is not None),
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
                            "selection": (
                                {"origin": "unseen_tiered_gated_high"}
                                if (run.run_purpose or THRESHOLD_EVALUATION)
                                == HIGH_TIER_VALIDATION
                                else {}
                            ),
                        }
                    ),
                    "needs_double_review": int(pair_key in doubles),
                    "double_review_reason": "sampled" if pair_key in doubles else "",
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
                    "run_purpose": run.run_purpose or THRESHOLD_EVALUATION,
                    "baseline": "registration_fuzzymachingscript",
                    "baseline_formula_sha256": {
                        source: hashlib.sha256(formula.encode()).hexdigest()
                        for source, formula in sorted(formulas.items())
                    },
                    "blocking": BLOCKING_VERSION,
                    "tiered": policy.version,
                    "splink": dependency_versions(),
                    "splink_adapter": SPLINK_ADAPTER_VERSION,
                    "splink_random_match_prior": RANDOM_MATCH_PRIOR,
                    "splink_training_record_count": splink_training_count,
                    "splink_training_record_limit": MAX_SPLINK_TRAINING_RECORDS,
                    "splink_direct_scoring_pair_limit": MAX_DIRECT_SCORING_PAIRS,
                    "splink_scored_sample_pairs": sum(
                        int(pair in probabilities) for pair in requested_pairs
                    ),
                    "splink_status": "local" if not probability_warning else "unavailable",
                    "splink_warning": probability_warning,
                    "benchmark_sample_pairs": len(benchmark_metadata),
                    "benchmark_candidate_recovered": sum(
                        item["candidate_recovered"] for item in benchmark_metadata.values()
                    ),
                    "high_tier_validation_population": high_validation_metadata,
                    "historical_evaluation_pair_keys": len(historical_pair_keys),
                    "historical_candidate_pairs_excluded": historical_candidate_exclusions,
                    "eligible_candidate_pairs": sum(eligible_source_pair_counts.values()),
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


def repair_run_probabilistic_scores(run_name: str) -> None:
    """Recompute only optional local-model scores for an existing review set.

    This operational repair deliberately preserves the snapshot, sampled pairs,
    deterministic scores, and review assignments. It is useful after fixing an
    adapter/runtime problem and never touches the production matching table.
    """
    run = frappe.get_doc("CCD Match Evaluation Run", run_name)
    try:
        if run.status not in {"Scoring", "Reviewing"}:
            frappe.throw("Probability repair is only allowed before evaluation finalization")
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
        pairs = frappe.get_all(
            "CCD Match Evaluation Pair",
            filters={"evaluation_run": run.name},
            fields=["name", "left_record", "right_record"],
        )
        required_ids = {
            record_id
            for pair in pairs
            for record_id in (str(pair.left_record), str(pair.right_record))
        }
        requested_pairs = {
            tuple(sorted((str(pair.left_record), str(pair.right_record))))
            for pair in pairs
        }
        probabilities, probability_warning, splink_training_count = _probability_map(
            records,
            policy,
            required_ids,
            requested_pairs,
        )
        scored_count = 0
        for pair in pairs:
            key = tuple(sorted((str(pair.left_record), str(pair.right_record))))
            probability = probabilities.get(key)
            if probability is not None:
                scored_count += 1
            frappe.db.set_value(
                "CCD Match Evaluation Pair",
                pair.name,
                {
                    "probabilistic_score": probability if probability is not None else 0,
                    "probabilistic_available": int(probability is not None),
                },
                update_modified=False,
            )

        versions = json.loads(run.model_versions_json or "{}")
        versions.update(
            {
                "splink": dependency_versions(),
                "splink_adapter": SPLINK_ADAPTER_VERSION,
                "splink_random_match_prior": RANDOM_MATCH_PRIOR,
                "splink_training_record_count": splink_training_count,
                "splink_training_record_limit": MAX_SPLINK_TRAINING_RECORDS,
                "splink_direct_scoring_pair_limit": MAX_DIRECT_SCORING_PAIRS,
                "splink_status": "local" if not probability_warning else "unavailable",
                "splink_warning": probability_warning,
                "splink_scored_sample_pairs": scored_count,
            }
        )
        run.db_set("model_versions_json", _json(versions), update_modified=False)
        run.db_set("status", "Reviewing", update_modified=False)
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        run = frappe.get_doc("CCD Match Evaluation Run", run_name)
        try:
            versions = json.loads(run.model_versions_json or "{}")
        except (TypeError, ValueError):
            versions = {}
        versions.update(
            {
                "splink_status": "unavailable",
                "splink_warning": f"probability_repair_failed:{type(exc).__name__}",
            }
        )
        run.db_set("model_versions_json", _json(versions), update_modified=False)
        run.db_set("status", "Reviewing", update_modified=False)
        frappe.log_error(traceback.format_exc(), "CCD Match Probability Repair Failure")
        frappe.db.commit()


def install_probability_repair(
    run_name: str,
    recover_stalled: bool = False,
) -> dict[str, str]:
    """Bench-only launcher for repairing optional scores on the long queue.

    ``recover_stalled`` is an explicit operator override for a run left in
    Scoring after its worker was externally terminated. Confirm that no job is
    active before using it; normal retries require the Reviewing state.
    """
    run = frappe.get_doc("CCD Match Evaluation Run", run_name)
    recovering = run.status == "Scoring" and bool(frappe.utils.cint(recover_stalled))
    if run.status != "Reviewing" and not recovering:
        frappe.throw("Only a run awaiting human review may be repaired")
    if recovering:
        versions = json.loads(run.model_versions_json or "{}")
        versions.update(
            {
                "splink_status": "unavailable",
                "splink_warning": "previous_probability_repair_worker_terminated",
            }
        )
        run.db_set("model_versions_json", _json(versions), update_modified=False)
    run.db_set("status", "Scoring", update_modified=False)
    frappe.db.commit()
    frappe.enqueue(
        "db_connector.api_fuzzy_evaluation.repair_run_probabilistic_scores",
        queue="long",
        timeout=14_400,
        enqueue_after_commit=True,
        run_name=run.name,
    )
    return {"run": run.name, "status": "Scoring"}


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
    if pair.review_status in {"Agreed", "Needs Adjudication", "Adjudicated"}:
        frappe.throw("This pair is already closed to ordinary review")
    existing = next(
        (row for row in pair.review_labels if row.reviewer == current_user and not row.is_adjudication),
        None,
    )
    if existing:
        frappe.throw(
            "Your review is already recorded and cannot be replaced; "
            "disagreements must be resolved by adjudication"
        )
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
    if label == "Same":
        # Every observed positive requires independent confirmation, even when
        # it was not part of the pre-assigned double-review sample.  The reason
        # is manager-only so the second reviewer remains blinded.
        _mark_positive_confirmation_required(pair)
    adjudicated_same = any(
        row.is_adjudication and row.label == "Same" for row in pair.review_labels
    )
    if adjudicated_same:
        _mark_positive_confirmation_required(pair)
        if _positive_confirmation_complete(pair.review_labels):
            pair.review_status = "Adjudicated"
            pair.final_label = "Same"
        elif label != "Same":
            # A new independent reviewer who does not confirm the prior Same
            # adjudication sends the pair back to management; otherwise the
            # pair would remain stuck without an available adjudication path.
            pair.review_status = "Needs Adjudication"
            pair.final_label = ""
        else:
            pair.review_status = POSITIVE_CONFIRMATION_REQUIRED
            pair.final_label = ""
        pair.save(ignore_permissions=True)
        frappe.db.commit()
        return {"pair": pair.name, "status": pair.review_status}
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
    if label == "Same":
        _mark_positive_confirmation_required(pair)
        if _positive_confirmation_complete(pair.review_labels):
            pair.final_label = "Same"
            pair.review_status = "Adjudicated"
        else:
            pair.final_label = ""
            pair.review_status = POSITIVE_CONFIRMATION_REQUIRED
    else:
        pair.final_label = "Different"
        pair.review_status = "Adjudicated"
    pair.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "pair": pair.name,
        "status": pair.review_status,
        "final_label": pair.final_label or "",
    }


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


def install_review_reason_repair(run_name: str) -> dict[str, int | str]:
    """Restore deterministic randomized-review reasons before finalization.

    This bench-only repair changes no human label or final decision. It is
    needed for runs created before Same adjudication stopped overwriting the
    original ``sampled`` assignment reason.
    """
    run = frappe.get_doc("CCD Match Evaluation Run", run_name)
    if run.status != "Reviewing":
        frappe.throw("Review-reason repair requires a run in Reviewing status")
    pairs = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={"evaluation_run": run.name},
        fields=[
            "name",
            "left_record",
            "right_record",
            "source_pair",
            "needs_double_review",
            "double_review_reason",
        ],
    )
    pair_by_key = {
        f"{pair.left_record}::{pair.right_record}": pair
        for pair in pairs
    }
    sampled_keys = double_review_ids(
        [
            SimpleNamespace(
                pair=CandidatePair(
                    str(pair.left_record),
                    str(pair.right_record),
                    str(pair.source_pair),
                    (),
                )
            )
            for pair in pairs
        ],
        int(run.double_review_count),
        seed=run.name,
    )
    restored = 0
    for key in sampled_keys:
        pair = pair_by_key.get(key)
        if not pair:
            frappe.throw("The randomized double-review assignment cannot be reconstructed")
        if pair.needs_double_review and pair.double_review_reason == "sampled":
            continue
        frappe.db.set_value(
            "CCD Match Evaluation Pair",
            pair.name,
            {
                "needs_double_review": 1,
                "double_review_reason": "sampled",
            },
            update_modified=False,
        )
        restored += 1

    versions = json.loads(run.model_versions_json or "{}")
    versions["sampled_double_review_reason_repair"] = {
        "assigned_pairs": len(sampled_keys),
        "restored_pairs": restored,
    }
    run.db_set("model_versions_json", _json(versions), update_modified=False)
    frappe.db.commit()
    return {
        "run": run.name,
        "assigned_pairs": len(sampled_keys),
        "restored_pairs": restored,
    }


def _metrics_dict(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    output = dict(value.__dict__)
    output["precision_wilson_95"] = wilson_interval(
        value.true_positive,
        value.true_positive + value.false_positive,
    )
    output["recall_wilson_95"] = wilson_interval(
        value.true_positive,
        value.true_positive + value.false_negative,
    )
    return output


def _calibrate_scores(
    pairs: list[Any],
    score_field: str,
    policy: MatchingPolicy,
) -> dict[str, Any]:
    calibration: list[tuple[bool, float]] = []
    held_out: list[tuple[bool, float]] = []
    for pair in pairs:
        score = pair.get(score_field)
        if score is None or (
            score_field == "probabilistic_score" and not pair.get("probabilistic_available")
        ):
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
    calibration_positives = sum(label for label, _ in calibration)
    held_out_positives = sum(label for label, _ in held_out)
    minimum_positives = policy.minimum_positive_labels_per_split
    validation_ready = (
        calibration_positives >= minimum_positives
        and held_out_positives >= minimum_positives
    )
    review_threshold = selection.review_threshold if validation_ready else None
    if not validation_ready:
        high_threshold = None
    warning = selection.warning if high_threshold is not None else "high_tier_disabled"
    if not validation_ready:
        warning = "insufficient_positive_labels_per_split"
    return {
        "available_pairs": len(calibration) + len(held_out),
        "calibration_pairs": len(calibration),
        "held_out_pairs": len(held_out),
        "calibration_positives": calibration_positives,
        "held_out_positives": held_out_positives,
        "minimum_positive_labels_per_split": minimum_positives,
        "validation_ready": validation_ready,
        "high_threshold": high_threshold,
        "review_threshold": review_threshold,
        "candidate_review_threshold": selection.review_threshold,
        "calibration_high": _metrics_dict(selection.high_metrics),
        "calibration_review": _metrics_dict(selection.review_metrics),
        "held_out_high": _metrics_dict(held_high),
        "held_out_review": _metrics_dict(held_review),
        "warning": warning,
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
    if tiered_tier in {MatchTier.HIGH.value, MatchTier.REVIEW.value}:
        return MatchTier.REVIEW.value
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
            "probabilistic_available",
            "double_review_reason",
            "benchmark_origin",
            "candidate_recovered",
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

    positive_pair_names = [pair.name for pair in pairs if pair.final_label == "Same"]
    positive_reviewers: dict[str, set[str]] = {name: set() for name in positive_pair_names}
    if positive_pair_names:
        for label in frappe.get_all(
            "CCD Match Review Label",
            filters={
                "parent": ["in", positive_pair_names],
                "label": "Same",
            },
            fields=["parent", "reviewer"],
        ):
            positive_reviewers[label.parent].add(label.reviewer)
    unconfirmed_positives = [
        name for name, reviewers in positive_reviewers.items() if len(reviewers) < 2
    ]
    if unconfirmed_positives:
        frappe.throw(
            f"{len(unconfirmed_positives)} Same pairs still require independent positive confirmation"
        )

    baseline_calibration = _calibrate_scores(pairs, "baseline_score", policy)
    probabilistic_calibration = _calibrate_scores(pairs, "probabilistic_score", policy)
    high_threshold = probabilistic_calibration["high_threshold"]
    review_threshold = probabilistic_calibration["review_threshold"]
    run_purpose = run.run_purpose or THRESHOLD_EVALUATION
    if run_purpose in {POSITIVE_BENCHMARK, HIGH_TIER_VALIDATION}:
        # Targeted cohorts are useful for a specific conditional question, but
        # cannot calibrate a prevalence-dependent deployable threshold.
        high_threshold = None
        review_threshold = None
        for calibration in (baseline_calibration, probabilistic_calibration):
            calibration["high_threshold"] = None
            calibration["review_threshold"] = None
            calibration["validation_ready"] = False
            calibration["warning"] = (
                "positive_benchmark_nonrepresentative"
                if run_purpose == POSITIVE_BENCHMARK
                else "high_tier_validation_nonrepresentative"
            )
    for pair in pairs:
        pair.hybrid_tier = _hybrid_tier(
            pair.tiered_tier,
            pair.probabilistic_score if pair.probabilistic_available else None,
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

    double_labels: list[tuple[str, str]] = []
    double_patterns: dict[str, int] = {}
    double_pairs = frappe.get_all(
        "CCD Match Evaluation Pair",
        filters={"evaluation_run": run.name, "needs_double_review": 1},
        fields=["name", "final_label", "double_review_reason"],
    )
    randomized_double_pairs = [
        pair for pair in double_pairs if pair.double_review_reason == "sampled"
    ]
    if len(randomized_double_pairs) != int(run.double_review_count):
        frappe.throw(
            "The randomized double-review assignment is incomplete; "
            "repair its reason metadata before finalization"
        )
    for pair in randomized_double_pairs:
        labels = frappe.get_all(
            "CCD Match Review Label",
            filters={"parent": pair.name, "is_adjudication": 0},
            pluck="label",
            order_by="idx asc",
        )
        if len(labels) >= 2:
            double_labels.append((labels[0], labels[1]))
            pattern = f"{labels[0]} / {labels[1]}"
            double_patterns[pattern] = double_patterns.get(pattern, 0) + 1

    double_agreed = sum(left == right for left, right in double_labels)
    agreement = {
        "assigned_pairs": len(randomized_double_pairs),
        "completed_pairs": len(double_labels),
        "agreement_rate": double_agreed / len(double_labels) if double_labels else None,
        "cohens_kappa": cohens_kappa(double_labels),
        "label_patterns": dict(sorted(double_patterns.items())),
        "total_double_review_pairs": len(double_pairs),
        "positive_confirmation_pairs": sum(
            pair.double_review_reason == "positive_confirmation" for pair in double_pairs
        ),
        "sampled_same_pairs": sum(
            pair.final_label == "Same" for pair in randomized_double_pairs
        ),
        "confirmed_same_pairs": sum(pair.final_label == "Same" for pair in double_pairs),
    }

    readiness_reasons = []
    if run_purpose == POSITIVE_BENCHMARK:
        readiness_reasons.append("positive_benchmark_nonrepresentative")
    if run_purpose == HIGH_TIER_VALIDATION:
        readiness_reasons.append("high_tier_validation_nonrepresentative")
    if run.candidate_truncated:
        readiness_reasons.append("candidate_generation_truncated")
    if json.loads(run.skipped_blocks_json or "[]"):
        readiness_reasons.append("oversized_blocks_skipped")
    if not probabilistic_calibration["validation_ready"]:
        readiness_reasons.append("insufficient_positive_labels_per_split")
    if probabilistic_calibration["high_threshold"] is None:
        readiness_reasons.append("automatic_high_threshold_disabled")

    benchmark_pairs = [pair for pair in pairs if pair.benchmark_origin]
    confirmed_benchmark_same = [pair for pair in benchmark_pairs if pair.final_label == "Same"]
    recovered_benchmark_same = sum(
        bool(pair.candidate_recovered) for pair in confirmed_benchmark_same
    )
    blocking_benchmark = None
    if benchmark_pairs:
        blocking_benchmark = {
            "discovery_only": True,
            "labeled_pairs": len(benchmark_pairs),
            "confirmed_same_pairs": len(confirmed_benchmark_same),
            "recovered_same_pairs": recovered_benchmark_same,
            "blocking_recall": (
                recovered_benchmark_same / len(confirmed_benchmark_same)
                if confirmed_benchmark_same
                else None
            ),
            "blocking_recall_wilson_95": (
                wilson_interval(recovered_benchmark_same, len(confirmed_benchmark_same))
                if confirmed_benchmark_same
                else None
            ),
        }

    high_validation = None
    if run_purpose == HIGH_TIER_VALIDATION:
        versions = json.loads(run.model_versions_json or "{}")
        high_validation = {
            "selection_population": versions.get("high_tier_validation_population") or {},
            "all_sampled_pairs_were_high": all(
                pair.tiered_tier == MatchTier.HIGH.value for pair in pairs
            ),
            "precision": _fixed_tier_metrics(
                pairs, "tiered_tier", {MatchTier.HIGH.value}
            )["all_labeled"],
        }

    metrics = {
        "run_purpose": run_purpose,
        "labeled_pairs": len(pairs),
        "reviewer_kappa": agreement["cohens_kappa"],
        "reviewer_agreement": agreement,
        "automatic_matching_readiness": {
            "ready": not readiness_reasons,
            "reasons": readiness_reasons,
        },
        "blocking_benchmark": blocking_benchmark,
        "high_tier_validation": high_validation,
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
