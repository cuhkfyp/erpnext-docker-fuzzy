"""Output protection for sensitive matching evidence."""

from __future__ import annotations

import html
from typing import Any

SENSITIVE_KEYS = {"hkid", "hkid_num", "government_id", "identifier", "id_value"}


def mask_identifier(value: Any, visible_suffix: int = 3) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    suffix = raw[-visible_suffix:] if len(raw) > visible_suffix else raw[-1:]
    return "*" * max(4, len(raw) - len(suffix)) + suffix


def safe_html(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def redact(value: Any, *, key: str = "") -> Any:
    if key.casefold() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: redact(item_value, key=item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, key=key) for item in value)
    return value
