"""Pure helpers for zero-write lower-score hybrid research.

This module deliberately does not authorize materialization.  It classifies
already-scored Splink Review pairs into explainable deterministic
corroboration strata so a prospective rule can later be frozen and validated.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import normalization as norm
from .types import EvidenceLevel


CONFIRMED_BIRTHDAY_KEYS = frozenset(
    {"1", "true", "yes", "y", "confirmed", "exact"}
)
STRONG_NAME_LEVELS = frozenset(
    {
        EvidenceLevel.EXACT.value,
        EvidenceLevel.CLOSE.value,
        EvidenceLevel.PHONETIC.value,
    }
)


def _level(evidence: Mapping[str, Any], attribute: str) -> str:
    item = evidence.get(attribute)
    value = getattr(item, "level", None)
    return str(getattr(value, "value", value) or "missing")


def birthday_key_confirmed(value: Any) -> bool:
    """Return whether a source explicitly marks its birthday as confirmed."""
    return norm.text(value).casefold() in CONFIRMED_BIRTHDAY_KEYS


def quality_aware_birthday(
    left_value: Any,
    right_value: Any,
    left_key: Any,
    right_key: Any,
) -> dict[str, Any]:
    """Classify asymmetric CCD birthday evidence.

    Matching normalized values are positive even when a source represents an
    uncertain date as ``YYYY-01-01``.  A mismatch is negative only when both
    sources explicitly confirm their birthdays; otherwise it is neutral.
    """
    left = norm.birthday(left_value)
    right = norm.birthday(right_value)
    left_confirmed = birthday_key_confirmed(left_key)
    right_confirmed = birthday_key_confirmed(right_key)
    if not left or not right:
        status = "birthday_missing"
        positive = conflict = False
    elif left == right:
        status = (
            "birthday_exact_confirmed"
            if left_confirmed and right_confirmed
            else "birthday_exact_unconfirmed"
        )
        positive = True
        conflict = False
    elif left_confirmed and right_confirmed:
        status = "birthday_mismatch_confirmed"
        positive = False
        conflict = True
    else:
        status = "birthday_mismatch_uncertain_neutral"
        positive = conflict = False
    return {
        "status": status,
        "positive": positive,
        "conflict": conflict,
        "left_confirmed": left_confirmed,
        "right_confirmed": right_confirmed,
    }


def classify_corroboration(
    evidence: Mapping[str, Any],
    *,
    left_birthday: Any,
    right_birthday: Any,
    left_birthday_key: Any,
    right_birthday_key: Any,
    trusted_identifiers: Iterable[str] = (),
) -> dict[str, Any]:
    """Return explainable research routes without making an identity decision."""
    trusted = frozenset(str(item) for item in trusted_identifiers)
    chinese_exact = all(
        _level(evidence, attribute) == EvidenceLevel.EXACT.value
        for attribute in ("chi_surname", "chi_firstname")
    )
    english_exact = all(
        _level(evidence, attribute) == EvidenceLevel.EXACT.value
        for attribute in ("eng_surname", "eng_firstname")
    )
    chinese_strong = all(
        _level(evidence, attribute) in STRONG_NAME_LEVELS
        for attribute in ("chi_surname", "chi_firstname")
    )
    english_strong = all(
        _level(evidence, attribute) in STRONG_NAME_LEVELS
        for attribute in ("eng_surname", "eng_firstname")
    )
    exact_name = chinese_exact or english_exact
    strong_name = chinese_strong or english_strong
    name_support = any(
        _level(evidence, attribute)
        in {
            EvidenceLevel.EXACT.value,
            EvidenceLevel.CLOSE.value,
            EvidenceLevel.PHONETIC.value,
            EvidenceLevel.WEAK.value,
        }
        for attribute in (
            "chi_surname",
            "chi_firstname",
            "eng_surname",
            "eng_firstname",
        )
    )

    birthday = quality_aware_birthday(
        left_birthday,
        right_birthday,
        left_birthday_key,
        right_birthday_key,
    )
    exact_secondary = []
    if birthday["positive"]:
        exact_secondary.append("birthday")
    for attribute in ("phone", "email"):
        if _level(evidence, attribute) == EvidenceLevel.EXACT.value:
            exact_secondary.append(attribute)

    trusted_exact = sorted(
        attribute
        for attribute in trusted
        if _level(evidence, attribute) == EvidenceLevel.EXACT.value
    )
    trusted_conflicts = sorted(
        attribute
        for attribute in trusted
        if _level(evidence, attribute) == EvidenceLevel.DISAGREE.value
    )
    unverified_exact = sorted(
        attribute
        for attribute in ("hkid", "hksr_num")
        if attribute not in trusted
        and _level(evidence, attribute) == EvidenceLevel.EXACT.value
    )

    routes = []
    if exact_name and birthday["positive"]:
        routes.append(f"exact_name+{birthday['status']}")
    if exact_name and "phone" in exact_secondary:
        routes.append("exact_name+phone_exact")
    if exact_name and "email" in exact_secondary:
        routes.append("exact_name+email_exact")
    if exact_name and unverified_exact:
        routes.append("exact_name+unverified_identifier_exact")
    if strong_name and len(exact_secondary) >= 2:
        routes.append("strong_name+two_secondary_exact")
    if strong_name and unverified_exact and exact_secondary:
        routes.append("strong_name+unverified_identifier+secondary_exact")
    elif name_support and unverified_exact and exact_secondary:
        routes.append("name_support+unverified_identifier+secondary_exact")
    if not strong_name and name_support and len(exact_secondary) >= 2:
        routes.append("name_support+two_secondary_exact")
    if strong_name and trusted_exact:
        routes.append("strong_name+trusted_identifier_exact")

    hard_conflicts = []
    if birthday["conflict"]:
        hard_conflicts.append("birthday_mismatch_confirmed")
    hard_conflicts.extend(
        f"trusted_identifier_conflict:{attribute}"
        for attribute in trusted_conflicts
    )
    return {
        "selected_for_research": bool(routes) and not hard_conflicts,
        "routes": tuple(sorted(set(routes))),
        "hard_conflicts": tuple(sorted(set(hard_conflicts))),
        "birthday_status": birthday["status"],
        "exact_name": exact_name,
        "strong_name": strong_name,
        "name_support": name_support,
        "exact_secondary": tuple(sorted(exact_secondary)),
        "trusted_identifier_exact": tuple(trusted_exact),
        "unverified_identifier_exact": tuple(unverified_exact),
    }


def lower_score_band(score: float, ultra_high_cutoff: float) -> str:
    value = float(score)
    cutoff = float(ultra_high_cutoff)
    if value >= cutoff:
        return "at_or_above_ultra_high"
    if value >= 0.999:
        return "0.999_to_ultra_high"
    if value >= 0.99:
        return "0.99_to_0.999"
    if value >= 0.95:
        return "0.95_to_0.99"
    return "below_0.95"
