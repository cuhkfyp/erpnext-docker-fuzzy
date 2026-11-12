"""Deterministic normalization helpers.

These helpers deliberately return an empty string for missing/invalid input so
callers can distinguish missing evidence from an observed disagreement.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_NON_ID = re.compile(r"[^0-9A-Z]+")
_HK_PHONE_INITIAL_DIGITS = frozenset("23456789")
_SEQUENTIAL_DIGIT_RUNS = ("0123456789", "9876543210")
_PHONE_HARD_SEPARATOR = re.compile(r"[/,;|\n\r]+")


def text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def simplified_chinese(value: Any) -> str:
    value = text(value)
    if not value:
        return ""
    try:
        from hanziconv import HanziConv

        return HanziConv.toSimplified(value)
    except Exception:
        return value


def chinese_compact(value: Any) -> str:
    return re.sub(r"[\s\W_]+", "", simplified_chinese(value), flags=re.UNICODE)


def chinese_pinyin(value: Any) -> str:
    value = simplified_chinese(value)
    if not value:
        return ""
    try:
        from pypinyin import lazy_pinyin

        return " ".join(lazy_pinyin(value)).lower()
    except Exception:
        return ""


def english_words(value: Any) -> str:
    value = text(value).casefold()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(_NON_ALNUM.sub(" ", value).split())


def english_compact(value: Any) -> str:
    return english_words(value).replace(" ", "")


def english_initials(value: Any) -> str:
    return "".join(word[0] for word in english_words(value).split() if word)


def phone(value: Any) -> str:
    """Return the legacy single configured phone value.

    This behaviour is intentionally frozen for policies that pre-date the
    multi-field phone strategy.  In particular, an existing policy continues
    to interpret one configured field exactly as it did when it was approved.
    New policies use :func:`phone_tokens` through a collection-valued canonical
    phone attribute.
    """
    raw = text(value)
    if not raw:
        return ""
    digits = "".join(re.findall(r"\d", raw))
    if digits.startswith("00852"):
        digits = digits[5:]
    elif digits.startswith("852") and len(digits) > 8:
        digits = digits[3:]
    if len(digits) != 8 or digits[0] not in _HK_PHONE_INITIAL_DIGITS:
        return ""
    if any(digits in run for run in _SEQUENTIAL_DIGIT_RUNS):
        return ""
    return digits


def _valid_phone_digits(raw_digits: Any) -> str:
    digits = "".join(re.findall(r"\d", text(raw_digits)))
    if digits.startswith("00852"):
        digits = digits[5:]
    elif digits.startswith("852") and len(digits) > 8:
        digits = digits[3:]
    # CCD centres record Hong Kong subscriber numbers. Reject incomplete,
    # foreign, and obvious sequential placeholder values before they can form
    # a large exact-phone block or act as independent identity evidence.
    if len(digits) != 8 or digits[0] not in _HK_PHONE_INITIAL_DIGITS:
        return ""
    if any(digits in run for run in _SEQUENTIAL_DIGIT_RUNS):
        return ""
    # Reject obvious repeated placeholders while retaining ordinary duplicated
    # digits in real subscriber numbers.
    if any(digits == digits[:size] * (8 // size) for size in (1, 2, 4)):
        return ""
    return digits


def phone_tokens(value: Any) -> tuple[str, ...]:
    """Return every distinct, plausible Hong Kong subscriber number.

    CCD phone fields commonly contain slash-separated relatives, emergency
    contacts, labels, spaces, or country prefixes.  Hard separators represent
    separate values; spaces and hyphens may format one number.  Invalid,
    foreign, sequential, and repeated placeholders never become evidence.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(
            sorted(
                {
                    token
                    for item in value
                    for token in phone_tokens(item)
                }
            )
        )
    raw = text(value)
    if not raw:
        return ()
    tokens: set[str] = set()
    for segment in _PHONE_HARD_SEPARATOR.split(raw):
        groups = re.findall(r"\d+", segment)
        if not groups:
            continue
        # Preserve complete digit groups when a free-text segment contains
        # more than one number; otherwise allow formatted 4+4 / country-code
        # groups to be combined.
        complete = [_valid_phone_digits(group) for group in groups]
        tokens.update(value for value in complete if value)
        combined = _valid_phone_digits("".join(groups))
        if combined:
            tokens.add(combined)
    return tuple(sorted(tokens))


def matching_phone_tokens(value: Any) -> tuple[str, ...]:
    """Return governed tokens without reinterpreting frozen scalar evidence.

    A tuple/list marks the new all-valid-fields strategy. A scalar marks the
    legacy configured-field strategy and therefore keeps its original
    normalization semantics.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        return phone_tokens(value)
    normalized = phone(value)
    return (normalized,) if normalized else ()


def email(value: Any) -> str:
    value = text(value).casefold()
    if not value or value.count("@") != 1:
        return ""
    local, domain = value.split("@", 1)
    if not local or "." not in domain:
        return ""
    return f"{local}@{domain}"


def birthday(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = text(value)
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def identifier(value: Any) -> str:
    return _NON_ID.sub("", text(value).upper())


def valid_hkid(value: Any) -> bool:
    """Validate the structure and check digit of a Hong Kong identity number."""
    normalized = identifier(value)
    match = re.fullmatch(r"([A-Z]{1,2})(\d{6})([0-9A])", normalized)
    if not match:
        return False
    prefix, digits, check = match.groups()
    values: list[int] = []
    if len(prefix) == 1:
        values.extend([36, ord(prefix) - 55])
    else:
        values.extend(ord(char) - 55 for char in prefix)
    total = values[0] * 9 + values[1] * 8
    total += sum(
        int(char) * weight for char, weight in zip(digits, range(7, 1, -1), strict=True)
    )
    total += 10 if check == "A" else int(check)
    return total % 11 == 0
