"""Pure parsing and validation helpers for operational notifications."""

from __future__ import annotations

import re
from typing import Any


_EMAIL_ADDRESS_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


def parse_notification_recipients(value: Any) -> tuple[str, ...]:
    """Parse a manager-maintained address list with stable de-duplication."""
    if isinstance(value, (list, tuple, set)):
        raw = [str(item or "").strip() for item in value]
    else:
        raw = re.split(r"[,;\s]+", str(value or ""))
    recipients: list[str] = []
    seen: set[str] = set()
    for item in raw:
        address = item.strip()
        key = address.casefold()
        if not address or key in seen:
            continue
        seen.add(key)
        recipients.append(address)
    return tuple(recipients)


def invalid_notification_recipients(value: Any) -> tuple[str, ...]:
    """Return malformed addresses; only plain mailbox addresses are accepted."""
    return tuple(
        address
        for address in parse_notification_recipients(value)
        if not _EMAIL_ADDRESS_RE.fullmatch(address)
    )


def is_orphaned_scheduler_job(
    status: Any,
    *,
    registered_in_queue: bool,
    age_seconds: float,
    minimum_age_seconds: float,
) -> bool:
    """Return whether RQ metadata claims a job that no live queue owns.

    The age gate makes the check fail closed while a newly queued job may be
    moving from its queue into a worker registry.
    """
    normalized_status = getattr(status, "value", status)
    return (
        str(normalized_status or "").casefold() in {"queued", "started"}
        and not registered_in_queue
        and age_seconds >= minimum_age_seconds
    )


def should_hold_email_backlog(
    pending_count: int,
    oldest_age_seconds: float,
    *,
    maximum_count: int,
    maximum_age_seconds: float,
) -> bool:
    """Fail closed before an automatic repair can release an old mail flood."""
    return pending_count > maximum_count or (
        pending_count > 0 and oldest_age_seconds > maximum_age_seconds
    )
