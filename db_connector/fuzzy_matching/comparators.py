"""Explainable comparison levels for person attributes."""

from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any

from . import normalization as norm
from .types import Evidence, EvidenceLevel


def _ratio(left: str, right: str) -> float:
    try:
        from rapidfuzz import fuzz

        return fuzz.ratio(left, right) / 100.0
    except Exception:
        return SequenceMatcher(None, left, right).ratio()


def _token_ratio(left: str, right: str) -> float:
    try:
        from rapidfuzz import fuzz

        return fuzz.token_set_ratio(left, right) / 100.0
    except Exception:
        return _ratio(" ".join(sorted(left.split())), " ".join(sorted(right.split())))


def _missing(attribute: str, left: str, right: str) -> Evidence | None:
    if left and right:
        return None
    return Evidence(
        attribute=attribute,
        level=EvidenceLevel.MISSING,
        score=0.0,
        left_present=bool(left),
        right_present=bool(right),
        reason="one_or_both_values_missing",
    )


def compare_chinese(attribute: str, left: Any, right: Any) -> Evidence:
    a, b = norm.chinese_compact(left), norm.chinese_compact(right)
    missing = _missing(attribute, a, b)
    if missing:
        return missing
    if a == b:
        return Evidence(attribute, EvidenceLevel.EXACT, 1.0, True, True, "normalized_hanzi_exact")
    pa, pb = norm.chinese_pinyin(a), norm.chinese_pinyin(b)
    if pa and pa == pb:
        return Evidence(attribute, EvidenceLevel.PHONETIC, 0.75, True, True, "pinyin_exact")
    score = max(_ratio(a, b), _token_ratio(pa, pb) if pa and pb else 0.0)
    if score >= 0.90:
        return Evidence(attribute, EvidenceLevel.CLOSE, 0.65, True, True, "chinese_close")
    if score >= 0.75:
        return Evidence(attribute, EvidenceLevel.WEAK, 0.30, True, True, "chinese_weak")
    return Evidence(attribute, EvidenceLevel.DISAGREE, 0.0, True, True, "chinese_disagree")


def compare_english(attribute: str, left: Any, right: Any) -> Evidence:
    words_a, words_b = norm.english_words(left), norm.english_words(right)
    missing = _missing(attribute, words_a, words_b)
    if missing:
        return missing
    if words_a == words_b:
        return Evidence(attribute, EvidenceLevel.EXACT, 1.0, True, True, "english_normalized_exact")
    compact_a, compact_b = norm.english_compact(words_a), norm.english_compact(words_b)
    if compact_a == compact_b:
        return Evidence(attribute, EvidenceLevel.EXACT, 0.97, True, True, "english_compact_exact")
    initials_a, initials_b = norm.english_initials(words_a), norm.english_initials(words_b)
    if len(initials_a) >= 2 and (initials_a == compact_b or initials_b == compact_a):
        return Evidence(attribute, EvidenceLevel.WEAK, 0.55, True, True, "english_initials")
    score = _token_ratio(words_a, words_b)
    if score >= 0.92:
        return Evidence(attribute, EvidenceLevel.CLOSE, 0.75, True, True, "english_close")
    if score >= 0.80:
        return Evidence(attribute, EvidenceLevel.WEAK, 0.35, True, True, "english_weak")
    return Evidence(attribute, EvidenceLevel.DISAGREE, 0.0, True, True, "english_disagree")


def compare_exact(
    attribute: str,
    left: Any,
    right: Any,
    normalizer: Callable[[Any], str],
) -> Evidence:
    a, b = normalizer(left), normalizer(right)
    missing = _missing(attribute, a, b)
    if missing:
        return missing
    if a == b:
        return Evidence(attribute, EvidenceLevel.EXACT, 1.0, True, True, f"{attribute}_exact")
    return Evidence(attribute, EvidenceLevel.DISAGREE, 0.0, True, True, f"{attribute}_disagree")


def compare_phone(attribute: str, left: Any, right: Any) -> Evidence:
    return compare_exact(attribute, left, right, norm.phone)


def compare_email(attribute: str, left: Any, right: Any) -> Evidence:
    return compare_exact(attribute, left, right, norm.email)


def compare_birthday(attribute: str, left: Any, right: Any) -> Evidence:
    return compare_exact(attribute, left, right, norm.birthday)


def compare_identifier(attribute: str, left: Any, right: Any) -> Evidence:
    return compare_exact(attribute, left, right, norm.identifier)
