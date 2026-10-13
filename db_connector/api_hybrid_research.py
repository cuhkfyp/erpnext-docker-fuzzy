"""Zero-write research preview for lower-score Splink plus corroboration.

The service deliberately reuses stored scores from the currently authorized
Review queue and never changes the frozen ultra-high cutoff, candidate state,
automation controls, or identity state.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any, Iterable

import frappe

from db_connector.api_fuzzy_evaluation import _evaluation_record_fields
from db_connector.fuzzy_matching.hybrid_research import (
    HYBRID_RESEARCH_RULE_VERSION,
    classify_corroboration,
    deterministic_stratified_sample,
    lower_score_band,
)
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.models import build_evidence
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.splink_automation import (
    FROZEN_AUTOMATIC_CUTOFF,
    canonical_source_pair,
    component_metadata,
)


SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
QUEUE_DOCTYPE = "CCD Match Review Queue Run"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
MEMBERSHIP_DOCTYPE = "CCD Identity Membership"
EXCLUSION_DOCTYPE = "CCD Identity Exclusion"
CURRENT_MEMBERSHIP_STATUSES = ("Active", "Needs Revalidation")
DEFAULT_RESEARCH_MINIMUM_SCORE = 0.95
MAX_RESEARCH_CANDIDATES = 100_000
MAX_EXAMPLES_PER_ROUTE = 5
MAX_EXPLORATORY_SAMPLE_SIZE = 250
QUERY_CHUNK_SIZE = 1_000


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _chunks(values: Iterable[str], size: int = QUERY_CHUNK_SIZE) -> Iterable[list[str]]:
    items = list(values)
    for start in range(0, len(items), max(1, int(size))):
        yield items[start : start + max(1, int(size))]


def _load_records(
    record_ids: set[str], policy: MatchingPolicy
) -> dict[str, dict[str, Any]]:
    fields = set(_evaluation_record_fields(policy))
    if frappe.get_meta("CCD Master").has_field("birthday_key"):
        fields.add("birthday_key")
    output: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(sorted(record_ids)):
        for row in frappe.get_all(
            "CCD Master",
            filters={"name": ["in", chunk]},
            fields=sorted(fields),
            limit_page_length=len(chunk),
        ):
            item = dict(row)
            item["source"] = str(item.get("ccd_reg_source") or "")
            output[str(item["name"])] = item
    return output


def _current_membership_records(record_ids: set[str]) -> set[str]:
    output: set[str] = set()
    for chunk in _chunks(sorted(record_ids)):
        output.update(
            str(value)
            for value in frappe.get_all(
                MEMBERSHIP_DOCTYPE,
                filters={
                    "ccd_master": ["in", chunk],
                    "status": ["in", list(CURRENT_MEMBERSHIP_STATUSES)],
                },
                pluck="ccd_master",
                limit_page_length=max(len(chunk) * 2, 1),
            )
        )
    return output


def _active_exclusion_pairs(record_ids: set[str]) -> set[tuple[str, str]]:
    output = set()
    for row in frappe.get_all(
        EXCLUSION_DOCTYPE,
        filters={"status": "Active"},
        fields=["left_record", "right_record"],
        limit_page_length=100_000,
    ):
        left, right = sorted((str(row.left_record), str(row.right_record)))
        if left in record_ids and right in record_ids:
            output.add((left, right))
    return output


def _untouched_reasons(row: dict[str, Any]) -> list[str]:
    reasons = []
    if row.get("stale"):
        reasons.append("candidate_marked_stale")
    if str(row.get("review_status") or "") != "Unreviewed" or row.get("final_label"):
        reasons.append("prior_human_review")
    if row.get("assigned_review_batch"):
        reasons.append("assigned_optional_review")
    if row.get("automation_reserved") or row.get("automation_batch"):
        reasons.append("reserved_or_batched")
    if str(row.get("automation_status") or "Optional Review") != "Optional Review":
        reasons.append("automation_lifecycle_unavailable")
    if str(row.get("materialization_status") or "Not Final") != "Not Final":
        reasons.append("materialization_started")
    return reasons


def _stable_example(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": str(row["name"]),
        "left_record": str(row["left_record"]),
        "right_record": str(row["right_record"]),
        "source_pair": canonical_source_pair(row.get("source_pair")),
        "score": float(row["probabilistic_score"]),
        "score_band": str(row["score_band"]),
        "birthday_status": str(row["birthday_status"]),
        "routes": list(row["routes"]),
    }


def _eligible_manifest_digest(rows: Iterable[dict[str, Any]]) -> str:
    material = []
    for row in rows:
        material.append(
            "\x1f".join(
                (
                    str(row.get("name") or ""),
                    str(row.get("left_identity_fingerprint") or ""),
                    str(row.get("right_identity_fingerprint") or ""),
                    f"{float(row.get('probabilistic_score') or 0):.12f}",
                    ",".join(sorted(str(route) for route in row.get("routes", ()))),
                )
            )
        )
    return hashlib.sha256("\n".join(sorted(material)).encode("utf-8")).hexdigest()


def _derived_exploratory_seed(
    queue: Any,
    cutoff: float,
    floor: float,
) -> str:
    payload = {
        "purpose": "lower-score-hybrid-exploratory-sample",
        "rule_version": HYBRID_RESEARCH_RULE_VERSION,
        "queue_run": str(queue.name),
        "matching_policy": str(queue.matching_policy or ""),
        "policy_version": str(queue.policy_version or ""),
        "policy_snapshot": str(queue.policy_snapshot_json or ""),
        "frozen_ultra_high_cutoff": f"{float(cutoff):.12f}",
        "research_minimum_score": f"{float(floor):.12f}",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stable_sample_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_order": int(row.get("sample_order") or 0),
        "candidate": str(row["name"]),
        "left_record": str(row["left_record"]),
        "right_record": str(row["right_record"]),
        "source_pair": canonical_source_pair(row.get("source_pair")),
        "score": float(row["probabilistic_score"]),
        "score_band": str(row["score_band"]),
        "birthday_status": str(row["birthday_status"]),
        "primary_route": str(row.get("primary_route") or ""),
        "routes": list(row["routes"]),
    }


def _classify_pair(
    left: dict[str, Any],
    right: dict[str, Any],
    policy: MatchingPolicy,
) -> dict[str, Any]:
    evidence = build_evidence(left, right, policy)
    trusted = {
        attribute
        for attribute in policy.trusted_global_identifiers
        if policy.globally_comparable(str(left["source"]), attribute)
        and policy.globally_comparable(str(right["source"]), attribute)
    }
    return classify_corroboration(
        evidence,
        left_birthday=policy.value(left, "birthday"),
        right_birthday=policy.value(right, "birthday"),
        left_birthday_key=left.get("birthday_key"),
        right_birthday_key=right.get("birthday_key"),
        trusted_identifiers=trusted,
    )


@frappe.whitelist()
def preview_lower_score_hybrid_candidate(candidate_name: str) -> dict[str, Any]:
    """Explain one Review candidate under the zero-write research rule."""
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, str(candidate_name or "").strip())
    if str(candidate.queue_run or "") != str(settings.authorized_splink_queue or ""):
        frappe.throw("Hybrid research must use the currently authorized Splink queue")
    queue = frappe.get_doc(QUEUE_DOCTYPE, candidate.queue_run)
    policy = MatchingPolicy.from_dict(json.loads(queue.policy_snapshot_json or "{}"))
    row = dict(candidate.as_dict())
    left_id = str(candidate.left_record)
    right_id = str(candidate.right_record)
    records = _load_records({left_id, right_id}, policy)
    left = records.get(left_id)
    right = records.get(right_id)
    if not left or not right:
        return {
            "zero_write": True,
            "research_only": True,
            "candidate": candidate.name,
            "selected_for_research": False,
            "unavailable_reasons": ["record_missing"],
        }

    unavailable = _untouched_reasons(row)
    if str(left.get("source") or "") == str(right.get("source") or ""):
        unavailable.append("same_source")
    if (
        str(left.get("modified") or "") != str(candidate.left_modified_at or "")
        or str(right.get("modified") or "")
        != str(candidate.right_modified_at or "")
    ):
        unavailable.append("source_modified_after_queue")
    if (
        identity_fingerprint(left, policy)
        != str(candidate.left_identity_fingerprint or "")
        or identity_fingerprint(right, policy)
        != str(candidate.right_identity_fingerprint or "")
    ):
        unavailable.append("identity_fingerprint_changed")

    result = _classify_pair(left, right, policy)
    record_ids = {left_id, right_id}
    memberships = _current_membership_records(record_ids)
    pair = tuple(sorted((left_id, right_id)))
    exclusions = _active_exclusion_pairs(record_ids)
    if memberships:
        unavailable.append("existing_group_overlap")
    if pair in exclusions:
        unavailable.append("active_human_exclusion")

    cutoff = float(settings.authorized_splink_cutoff or 0)
    score = float(candidate.probabilistic_score or 0)
    in_lower_score_band = float(queue.review_threshold or 0) <= score < cutoff
    if not in_lower_score_band:
        unavailable.append("outside_lower_score_research_band")
    return {
        "zero_write": True,
        "research_only": True,
        "authorizes_materialization": False,
        "candidate": str(candidate.name),
        "queue_run": str(queue.name),
        "left_record": left_id,
        "right_record": right_id,
        "source_pair": canonical_source_pair(candidate.source_pair),
        "probabilistic_score": score,
        "frozen_ultra_high_cutoff": cutoff,
        "score_band": lower_score_band(score, cutoff),
        "in_lower_score_band": in_lower_score_band,
        **result,
        "unavailable_reasons": sorted(set(unavailable)),
        "preliminary_operationally_clear": bool(
            result["selected_for_research"] and not unavailable
        ),
        "warnings": [
            "This explanation does not approve or materialize the candidate.",
            "Complete-component and prospective-validation gates are not evaluated by this single-pair preview.",
        ],
    }


@frappe.whitelist()
def preview_lower_score_hybrid_research(
    minimum_score: float | str = DEFAULT_RESEARCH_MINIMUM_SCORE,
    queue_run: str = "",
    exploratory_sample_size: int | str = 0,
) -> dict[str, Any]:
    """Classify the current lower-score pool without writing any state."""
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    authorized_queue = str(settings.authorized_splink_queue or "")
    requested_queue = str(queue_run or authorized_queue).strip()
    if not requested_queue or not frappe.db.exists(QUEUE_DOCTYPE, requested_queue):
        frappe.throw("An existing authorized Splink Review Queue is required")
    if requested_queue != authorized_queue:
        frappe.throw("Hybrid research must use the currently authorized Splink queue")
    queue = frappe.get_doc(QUEUE_DOCTYPE, requested_queue)
    if queue.status != "Ready":
        frappe.throw("Hybrid research requires a Ready Splink Review Queue")

    cutoff = float(settings.authorized_splink_cutoff or 0)
    if cutoff <= 0:
        frappe.throw("The frozen authorized Splink cutoff is missing")
    floor = float(minimum_score or DEFAULT_RESEARCH_MINIMUM_SCORE)
    review_threshold = float(queue.review_threshold or 0)
    if floor < review_threshold or floor >= cutoff:
        frappe.throw(
            f"Research minimum score must be at least {review_threshold:.12f} "
            f"and below {cutoff:.12f}"
        )
    requested_sample_size = int(exploratory_sample_size or 0)
    if requested_sample_size < 0 or requested_sample_size > MAX_EXPLORATORY_SAMPLE_SIZE:
        frappe.throw(
            f"Exploratory sample size must be between 0 and {MAX_EXPLORATORY_SAMPLE_SIZE}"
        )
    if requested_sample_size and requested_sample_size < 25:
        frappe.throw("Exploratory sample size must be at least 25")

    rows = [
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
            limit_page_length=MAX_RESEARCH_CANDIDATES,
        )
    ]
    if len(rows) >= MAX_RESEARCH_CANDIDATES:
        frappe.throw("Hybrid research candidate ceiling reached; narrow the score floor")

    policy = MatchingPolicy.from_dict(json.loads(queue.policy_snapshot_json or "{}"))
    record_ids = {
        str(record_id)
        for row in rows
        for record_id in (row["left_record"], row["right_record"])
    }
    records = _load_records(record_ids, policy)

    state_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    birthday_counts: Counter[str] = Counter()
    score_band_counts: Counter[str] = Counter()
    source_pair_counts: Counter[str] = Counter()
    retrospective_labels: dict[str, Counter[str]] = defaultdict(Counter)
    selected_rows: list[dict[str, Any]] = []
    all_corroborated_count = 0

    for row in rows:
        row["score_band"] = lower_score_band(
            float(row["probabilistic_score"]), cutoff
        )
        score_band_counts[row["score_band"]] += 1
        left_id = str(row["left_record"])
        right_id = str(row["right_record"])
        left = records.get(left_id)
        right = records.get(right_id)
        unavailable = _untouched_reasons(row)
        if not left or not right:
            unavailable.append("record_missing")
            state_counts.update(set(unavailable))
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
        row.update(result)
        birthday_counts[result["birthday_status"]] += 1
        if result["hard_conflicts"]:
            state_counts["hard_evidence_conflict"] += 1
        if not result["routes"]:
            state_counts["no_strong_corroboration"] += 1
        if not result["selected_for_research"]:
            continue

        all_corroborated_count += 1
        for route in result["routes"]:
            route_counts[route] += 1
            if row.get("final_label"):
                retrospective_labels[route][str(row["final_label"])] += 1
        if unavailable:
            state_counts.update(set(unavailable))
            continue
        row["left_record"] = left_id
        row["right_record"] = right_id
        selected_rows.append(row)

    graph = component_metadata(selected_rows)
    isolated_rows = [
        row for row in selected_rows if graph[str(row["name"])]["isolated_pair"]
    ]
    selected_record_ids = {
        str(record_id)
        for row in isolated_rows
        for record_id in (row["left_record"], row["right_record"])
    }
    membership_records = _current_membership_records(selected_record_ids)
    exclusion_pairs = _active_exclusion_pairs(selected_record_ids)
    operationally_clear = []
    for row in isolated_rows:
        pair = tuple(sorted((str(row["left_record"]), str(row["right_record"]))))
        if pair[0] in membership_records or pair[1] in membership_records:
            state_counts["existing_group_overlap"] += 1
            continue
        if pair in exclusion_pairs:
            state_counts["active_human_exclusion"] += 1
            continue
        operationally_clear.append(row)

    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in operationally_clear:
        for route in row["routes"]:
            if len(examples[route]) < MAX_EXAMPLES_PER_ROUTE:
                examples[route].append(_stable_example(row))

    component_fingerprints = {
        str(details["component_fingerprint"])
        for details in graph.values()
    }
    isolated_component_fingerprints = {
        str(graph[str(row["name"])]["component_fingerprint"])
        for row in isolated_rows
    }
    for row in operationally_clear:
        source_pair_counts[canonical_source_pair(row.get("source_pair"))] += 1

    exploratory_sample: dict[str, Any] | None = None
    if requested_sample_size:
        eligible_manifest_digest = _eligible_manifest_digest(operationally_clear)
        exploratory_seed = _derived_exploratory_seed(queue, cutoff, floor)
        sample = deterministic_stratified_sample(
            operationally_clear,
            requested_sample_size,
            exploratory_seed,
        )
        sample_rows = sample.pop("rows")
        sample_route_counts: Counter[str] = Counter()
        sample_primary_route_counts: Counter[str] = Counter()
        sample_score_band_counts: Counter[str] = Counter()
        sample_source_pair_counts: Counter[str] = Counter()
        sample_birthday_counts: Counter[str] = Counter()
        for row in sample_rows:
            sample_route_counts.update(str(route) for route in row["routes"])
            sample_primary_route_counts[str(row["primary_route"])] += 1
            sample_score_band_counts[str(row["score_band"])] += 1
            sample_source_pair_counts[canonical_source_pair(row.get("source_pair"))] += 1
            sample_birthday_counts[str(row["birthday_status"])] += 1
        exploratory_sample = {
            **sample,
            "rule_version": HYBRID_RESEARCH_RULE_VERSION,
            "derived_seed": exploratory_seed,
            "eligible_manifest_digest": eligible_manifest_digest,
            "route_counts": dict(sorted(sample_route_counts.items())),
            "primary_route_counts": dict(sorted(sample_primary_route_counts.items())),
            "score_band_counts": dict(sorted(sample_score_band_counts.items())),
            "source_pair_counts": dict(sorted(sample_source_pair_counts.items())),
            "birthday_evidence_counts": dict(sorted(sample_birthday_counts.items())),
            "rows": [_stable_sample_row(row) for row in sample_rows],
            "notes": [
                "The seed is derived from frozen queue/rule provenance; it is not user-selectable.",
                "The same eligible manifest and provenance produce the same sample.",
                "Rare routes are included in full when the sample target permits.",
                "Exploratory inspection cannot be counted as prospective validation.",
            ],
        }

    total_lower_score = frappe.db.count(
        CANDIDATE_DOCTYPE,
        filters={
            "queue_run": queue.name,
            "probabilistic_score": ["<", cutoff],
        },
    )
    return {
        "zero_write": True,
        "research_only": True,
        "authorizes_materialization": False,
        "uses_existing_stored_splink_scores": True,
        "changes_splink_model": False,
        "changes_authorized_cutoff": False,
        "queue_run": str(queue.name),
        "matching_policy": str(queue.matching_policy or ""),
        "policy_version": str(queue.policy_version or ""),
        "frozen_ultra_high_cutoff": cutoff,
        "code_frozen_cutoff": FROZEN_AUTOMATIC_CUTOFF,
        "review_threshold": review_threshold,
        "research_minimum_score": floor,
        "lower_score_candidate_count": int(total_lower_score),
        "scanned_candidate_count": len(rows),
        "corroborated_candidate_count": all_corroborated_count,
        "untouched_corroborated_edge_count": len(selected_rows),
        "research_component_count": len(component_fingerprints),
        "isolated_pair_count": len(isolated_rows),
        "incomplete_or_multi_record_component_count": (
            len(component_fingerprints) - len(isolated_component_fingerprints)
        ),
        "operationally_clear_isolated_pair_count": len(operationally_clear),
        "state_exclusion_counts": dict(sorted(state_counts.items())),
        "corroboration_route_counts": dict(sorted(route_counts.items())),
        "birthday_evidence_counts": dict(sorted(birthday_counts.items())),
        "score_band_counts": dict(sorted(score_band_counts.items())),
        "operationally_clear_source_pair_counts": dict(
            sorted(source_pair_counts.items())
        ),
        "retrospective_labels_by_route": {
            route: dict(sorted(labels.items()))
            for route, labels in sorted(retrospective_labels.items())
        },
        "examples_by_route": dict(sorted(examples.items())),
        "exploratory_sample": exploratory_sample,
        "warnings": [
            "This preview does not approve or materialize any lower-score pair.",
            "Retrospective labels are descriptive only and cannot authorize the future frozen rule.",
            "A prospective unseen validation and blinded holdout remain mandatory before automation.",
        ],
    }
