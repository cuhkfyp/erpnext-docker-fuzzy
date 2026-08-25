"""Pure helpers for bounded, complete identity-component corrections."""

from __future__ import annotations

import hashlib
import json
from itertools import combinations
from typing import Any, Iterable


MAX_CORRECTION_RECORDS = 25


def normalize_partition(
    record_ids: Iterable[str],
    groups: Iterable[Iterable[str]],
    *,
    maximum_records: int = MAX_CORRECTION_RECORDS,
) -> tuple[tuple[str, ...], ...]:
    """Return a deterministic, complete, non-overlapping identity partition."""
    records = tuple(sorted({str(item).strip() for item in record_ids if str(item).strip()}))
    if len(records) < 2:
        raise ValueError("A correction requires at least two CCD records")
    if len(records) > maximum_records:
        raise ValueError(
            f"A correction is limited to {maximum_records} records; this scope has {len(records)}"
        )

    normalized: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for raw_group in groups:
        if isinstance(raw_group, (str, bytes)):
            raise ValueError("Each replacement group must be a list of CCD record IDs")
        raw_values = list(raw_group)
        if any(not isinstance(item, str) or not item.strip() for item in raw_values):
            raise ValueError("Every replacement member must be a non-empty CCD record ID")
        raw_members = [item.strip() for item in raw_values]
        members = tuple(sorted(set(raw_members)))
        if not members:
            raise ValueError("Replacement groups cannot be empty")
        if len(members) != len(raw_members):
            raise ValueError("A CCD record cannot be repeated inside a replacement group")
        overlap = seen.intersection(members)
        if overlap:
            raise ValueError(
                "A CCD record cannot appear in more than one replacement group: "
                + ", ".join(sorted(overlap))
            )
        seen.update(members)
        normalized.append(members)

    missing = sorted(set(records) - seen)
    unexpected = sorted(seen - set(records))
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("outside correction scope: " + ", ".join(unexpected))
        raise ValueError("The replacement partition must cover the complete scope (" + "; ".join(details) + ")")
    return tuple(sorted(normalized))


def exclusions_for_partition(
    groups: Iterable[Iterable[str]],
) -> tuple[tuple[str, str], ...]:
    """Return every cross-partition Different pair in deterministic order."""
    normalized = tuple(tuple(sorted(str(item) for item in group)) for group in groups)
    exclusions = {
        tuple(sorted((left, right)))
        for left_group, right_group in combinations(normalized, 2)
        for left in left_group
        for right in right_group
    }
    return tuple(sorted(exclusions))


def stable_payload_fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def correction_key(
    source_decision: str,
    scope_fingerprint: str,
    replacement_groups: Iterable[Iterable[str]],
) -> str:
    groups = tuple(sorted(tuple(sorted(str(item) for item in group)) for group in replacement_groups))
    return stable_payload_fingerprint(
        {
            "source_decision": str(source_decision),
            "scope_fingerprint": str(scope_fingerprint),
            "replacement_groups": groups,
        }
    )


def partition_for_display(
    groups: Iterable[Iterable[str]],
    aliases: dict[str, str],
    *,
    reveal_record_ids: bool = False,
) -> tuple[list[list[dict[str, Any]]], int]:
    """Label a decision partition without leaking out-of-component record IDs."""
    outside_labels: dict[str, str] = {}
    displayed: list[list[dict[str, Any]]] = []
    for raw_group in groups:
        members: list[dict[str, Any]] = []
        for raw_record_id in raw_group:
            record_id = str(raw_record_id)
            in_review = record_id in aliases
            if in_review:
                label = aliases[record_id]
            else:
                if record_id not in outside_labels:
                    outside_labels[record_id] = (
                        f"Outside component record {len(outside_labels) + 1}"
                    )
                label = record_id if reveal_record_ids else outside_labels[record_id]
            member: dict[str, Any] = {
                "label": label,
                "in_original_review": in_review,
            }
            if reveal_record_ids:
                member["record_id"] = record_id
            members.append(member)
        if members:
            displayed.append(members)
    return displayed, len(outside_labels)
