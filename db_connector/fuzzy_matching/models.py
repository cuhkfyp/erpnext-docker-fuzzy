"""Baseline, deterministic, and hybrid comparison models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .comparators import (
    compare_birthday,
    compare_chinese,
    compare_email,
    compare_english,
    compare_identifier,
    compare_phone,
)
from . import normalization as norm
from .policy import MatchingPolicy
from .types import CandidatePair, EvaluationResult, Evidence, EvidenceLevel, MatchTier, ModelResult

BASELINE_WEIGHTS = {
    "chi_surname": 0.08,
    "chi_firstname": 0.42,
    "eng_surname": 0.10,
    "eng_firstname": 0.25,
    "phone": 0.15,
}


def build_evidence(
    left: dict[str, Any],
    right: dict[str, Any],
    policy: MatchingPolicy,
) -> dict[str, Evidence]:
    evidence = {
        "chi_surname": compare_chinese(
            "chi_surname", policy.value(left, "chi_surname"), policy.value(right, "chi_surname")
        ),
        "chi_firstname": compare_chinese(
            "chi_firstname", policy.value(left, "chi_firstname"), policy.value(right, "chi_firstname")
        ),
        "eng_surname": compare_english(
            "eng_surname", policy.value(left, "eng_surname"), policy.value(right, "eng_surname")
        ),
        "eng_firstname": compare_english(
            "eng_firstname", policy.value(left, "eng_firstname"), policy.value(right, "eng_firstname")
        ),
        "phone": compare_phone("phone", policy.value(left, "phone"), policy.value(right, "phone")),
        "email": compare_email("email", policy.value(left, "email"), policy.value(right, "email")),
        "birthday": compare_birthday(
            "birthday", policy.value(left, "birthday"), policy.value(right, "birthday")
        ),
        "hksr_num": compare_identifier(
            "hksr_num", policy.value(left, "hksr_num"), policy.value(right, "hksr_num")
        ),
        "hkid": compare_identifier(
            "hkid", policy.value(left, "hkid"), policy.value(right, "hkid")
        ),
    }
    left_source = str(left.get("source") or left.get("ccd_reg_source") or "")
    right_source = str(right.get("source") or right.get("ccd_reg_source") or "")
    for attribute in policy.trusted_global_identifiers:
        if not (
            policy.globally_comparable(left_source, attribute)
            and policy.globally_comparable(right_source, attribute)
        ):
            continue
        # The two built-in strong identifiers are already present. This branch
        # keeps policy extensions safe without changing their comparison rule.
        if attribute not in evidence:
            evidence[attribute] = compare_identifier(
                attribute, policy.value(left, attribute), policy.value(right, attribute)
            )
    return evidence


def baseline_result(evidence: dict[str, Evidence], threshold: float = 0.65) -> ModelResult:
    raw = sum(evidence[field].score * weight for field, weight in BASELINE_WEIGHTS.items())
    tier = MatchTier.REVIEW if raw > threshold else MatchTier.LOW
    return ModelResult(
        model="current_weighted_formula",
        score=round(raw, 4),
        tier=tier,
        reasons=(f"weighted_score_{raw:.4f}",),
        evidence=evidence,
    )


def _names(evidence: dict[str, Evidence]) -> tuple[bool, bool, bool]:
    chinese_exact = evidence["chi_surname"].exact and evidence["chi_firstname"].exact
    english_exact = evidence["eng_surname"].exact and evidence["eng_firstname"].exact
    name_support = any(
        item.level in {EvidenceLevel.EXACT, EvidenceLevel.CLOSE, EvidenceLevel.PHONETIC, EvidenceLevel.WEAK}
        for key, item in evidence.items()
        if key in {"chi_surname", "chi_firstname", "eng_surname", "eng_firstname"}
    )
    return chinese_exact, english_exact, name_support


def tiered_result(
    evidence: dict[str, Evidence],
    policy: MatchingPolicy,
    *,
    conflict_mode: str,
    trusted_identifiers: frozenset[str] = frozenset(),
) -> ModelResult:
    exact_ids = [
        attribute
        for attribute in trusted_identifiers
        if attribute in evidence and evidence[attribute].exact
    ]
    conflicting_ids = [
        attribute
        for attribute in trusted_identifiers
        if attribute in evidence and evidence[attribute].level == EvidenceLevel.DISAGREE
    ]
    chinese_exact, english_exact, name_support = _names(evidence)
    exact_secondary = [
        key for key in ("birthday", "phone", "email") if evidence[key].exact
    ]
    unverified_exact_ids = [
        key
        for key in ("hkid", "hksr_num")
        if key in evidence and evidence[key].exact and key not in trusted_identifiers
    ]
    name_disagreement = any(
        evidence[key].level == EvidenceLevel.DISAGREE
        for key in ("chi_surname", "chi_firstname", "eng_surname", "eng_firstname")
        if evidence[key].available
    )

    reasons: list[str] = []
    if exact_ids:
        reasons.extend(f"trusted_global_id_exact:{key}" for key in exact_ids)
    if conflicting_ids:
        reasons.extend(f"trusted_global_id_conflict:{key}" for key in conflicting_ids)
    if exact_secondary:
        reasons.extend(f"independent_exact:{key}" for key in exact_secondary)
    if unverified_exact_ids:
        reasons.extend(f"unverified_identifier_exact:{key}" for key in unverified_exact_ids)
    if chinese_exact:
        reasons.append("chinese_full_name_exact")
    if english_exact:
        reasons.append("english_full_name_exact")

    available = [item for item in evidence.values() if item.available]
    score = sum(item.score for item in available) / len(available) if available else 0.0

    if conflicting_ids:
        if conflict_mode == "recoverable" and name_support and exact_secondary:
            tier = MatchTier.REVIEW
            reasons.append("identifier_conflict_recovered_to_review_only")
        else:
            tier = MatchTier.CONFLICT
            reasons.append("identifier_conflict_gate")
    elif exact_ids:
        tier = MatchTier.HIGH
        if name_disagreement:
            reasons.append("name_conflict_warning")
    elif (chinese_exact or english_exact) and exact_secondary:
        tier = MatchTier.HIGH
        reasons.append("exact_name_plus_independent_evidence")
    elif name_support or exact_secondary or unverified_exact_ids:
        tier = MatchTier.REVIEW
        reasons.append("human_review_required")
        if not exact_secondary and not unverified_exact_ids:
            reasons.append("insufficient_independent_evidence")
    else:
        tier = MatchTier.LOW
        reasons.append("insufficient_evidence")

    return ModelResult(
        model=f"tiered_{conflict_mode}",
        score=round(score, 4),
        tier=tier,
        reasons=tuple(reasons),
        evidence=evidence,
    )


def hybrid_result(
    tiered: ModelResult,
    probability: float | None,
    *,
    high_threshold: float | None,
    review_threshold: float | None,
) -> ModelResult:
    if probability is None:
        return ModelResult("hybrid", None, tiered.tier, ("probability_unavailable", *tiered.reasons))
    if tiered.tier == MatchTier.CONFLICT:
        tier = MatchTier.CONFLICT
    elif high_threshold is not None and probability >= high_threshold and tiered.tier != MatchTier.LOW:
        tier = MatchTier.HIGH
    elif tiered.tier in {MatchTier.HIGH, MatchTier.REVIEW}:
        # A statistical model may add review candidates, but it must never
        # suppress independently sufficient deterministic review evidence.
        # An uncalibrated deterministic High remains review-only.
        tier = MatchTier.REVIEW
    elif review_threshold is not None and probability >= review_threshold:
        tier = MatchTier.REVIEW
    else:
        tier = MatchTier.LOW
    return ModelResult(
        "hybrid",
        round(probability, 6),
        tier,
        (f"probability:{probability:.6f}", *tiered.reasons),
        tiered.evidence,
        probability,
    )


def compare_all_models(
    pair: CandidatePair,
    left: dict[str, Any],
    right: dict[str, Any],
    policy: MatchingPolicy,
    *,
    probability: float | None = None,
    high_threshold: float | None = None,
    review_threshold: float | None = None,
) -> EvaluationResult:
    evidence = build_evidence(left, right, policy)
    left_source = str(left.get("source") or left.get("ccd_reg_source") or "")
    right_source = str(right.get("source") or right.get("ccd_reg_source") or "")
    trusted_identifiers = frozenset(
        attribute
        for attribute in policy.trusted_global_identifiers
        if policy.globally_comparable(left_source, attribute)
        and policy.globally_comparable(right_source, attribute)
        and (
            attribute != "hkid"
            or (
                norm.valid_hkid(policy.value(left, attribute))
                and norm.valid_hkid(policy.value(right, attribute))
            )
        )
    )
    baseline = baseline_result(evidence)
    gated = tiered_result(
        evidence,
        policy,
        conflict_mode="gated",
        trusted_identifiers=trusted_identifiers,
    )
    recoverable = tiered_result(
        evidence,
        policy,
        conflict_mode="recoverable",
        trusted_identifiers=trusted_identifiers,
    )
    probabilistic = None
    if probability is not None:
        probabilistic = ModelResult(
            "fellegi_sunter",
            round(probability, 6),
            MatchTier.REVIEW,
            ("unclassified_probability",),
            evidence,
            probability,
        )
    hybrid = hybrid_result(gated, probability, high_threshold=high_threshold, review_threshold=review_threshold)
    return EvaluationResult(pair, baseline, gated, recoverable, probabilistic, hybrid)
