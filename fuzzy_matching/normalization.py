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
    raw = text(value)
    if not raw:
        return ""
    digits = "".join(re.findall(r"\d", raw))
    if digits.startswith("00852"):
        digits = digits[5:]
    elif digits.startswith("852") and len(digits) > 8:
        digits = digits[3:]
    return digits


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
