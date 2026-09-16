"""Pure helpers for durable CCD Unified Person identifiers and lineage.

This module deliberately has no Frappe dependency.  The database service uses
these helpers for number issuance and deterministic merge/split reconciliation,
while unit tests can exercise the invariants without a live site.
"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from typing import Iterable, Mapping


UNIFIED_PERSON_PREFIX = "HKSR-U"
UNIFIED_PERSON_SEQUENCE_DIGITS = 9
UNIFIED_PERSON_MAX_SEQUENCE = 10**UNIFIED_PERSON_SEQUENCE_DIGITS - 1
_NUMBER_PATTERN = re.compile(r"^HKSR-U([0-9]{9})([0-9])$")


def luhn_check_digit(payload: str | int) -> int:
    """Return the Luhn digit for a nine-digit sequence payload."""
    raw = str(payload).strip()
    if not raw.isdigit() or len(raw) > UNIFIED_PERSON_SEQUENCE_DIGITS:
        raise ValueError("Unified Person sequence must contain at most nine digits")
    digits = raw.zfill(UNIFIED_PERSON_SEQUENCE_DIGITS) + "0"
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return (10 - total % 10) % 10


def format_unified_person_number(sequence: int) -> str:
    """Format one non-zero sequence as ``HKSR-U#########C``."""
    value = int(sequence)
    if value < 1 or value > UNIFIED_PERSON_MAX_SEQUENCE:
        raise ValueError("Unified Person sequence is outside the nine-digit range")
    payload = f"{value:0{UNIFIED_PERSON_SEQUENCE_DIGITS}d}"
    return f"{UNIFIED_PERSON_PREFIX}{payload}{luhn_check_digit(payload)}"


def parse_unified_person_number(value: str) -> int:
    """Validate a Unified Person number and return its sequence."""
    match = _NUMBER_PATTERN.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError("Invalid Unified Person number format")
    payload, supplied = match.groups()
    if int(payload) < 1 or luhn_check_digit(payload) != int(supplied):
        raise ValueError("Invalid Unified Person check digit")
    return int(payload)


def valid_unified_person_number(value: str) -> bool:
    try:
        parse_unified_person_number(value)
    except (TypeError, ValueError):
        return False
    return True


def source_lineage_key(governed_source: str, source_record_key: str) -> str:
    """Return the stable, non-PII key used across CCD Master recreation."""
    source = str(governed_source or "").strip()
    record = str(source_record_key or "").strip()
    if not source or not record:
        return ""
    return hashlib.sha256(
        f"unified-person-source-lineage-v1\x1f{source}\x1f{record}".encode()
    ).hexdigest()


def recreated_lineage_person(
    canonical_candidates: Iterable[str], active_other_records: Iterable[str]
) -> str:
    """Return the only safe number for a deleted-and-recreated source row.

    Reuse is allowed only when every historical number resolves to one
    canonical person and the stable source lineage is not still active on a
    different CCD Master.  An empty result means issue an independent
    singleton and require human governance rather than guessing.
    """
    candidates = {str(value) for value in canonical_candidates if str(value)}
    active_others = {str(value) for value in active_other_records if str(value)}
    if active_others or len(candidates) != 1:
        return ""
    return next(iter(candidates))


def _clusters(groups: Iterable[Iterable[str]]) -> tuple[tuple[str, ...], ...]:
    normalized = tuple(
        sorted(
            tuple(sorted({str(record_id) for record_id in group if str(record_id)}))
            for group in groups
        )
    )
    if any(not group for group in normalized):
        raise ValueError("Unified Person clusters cannot be empty")
    flattened = [record_id for group in normalized for record_id in group]
    if len(flattened) != len(set(flattened)):
        raise ValueError("A CCD Master appears in more than one Unified Person cluster")
    return normalized


@dataclass(frozen=True)
class LineagePlan:
    """Prior-number allocation for a replacement logical-person partition.

    ``cluster_people`` contains only reusable prior numbers.  A missing cluster
    must receive a newly issued number. ``alias_cluster`` maps every unused
    prior number to the cluster whose canonical number must resolve it.
    """

    clusters: tuple[tuple[str, ...], ...]
    cluster_people: Mapping[int, str]
    alias_cluster: Mapping[str, int]


def plan_lineage_reassignment(
    groups: Iterable[Iterable[str]],
    person_lineages: Mapping[str, Iterable[str]],
    person_sequences: Mapping[str, int],
) -> LineagePlan:
    """Choose stable numbers for merge/split results.

    A prior number whose lineage occurs in exactly one replacement cluster is
    unambiguous and is preferred there.  If a prior canonical lineage spans a
    split, the oldest remaining number survives in the lexicographically first
    compatible cluster.  Other clusters receive new numbers.  Every unused
    issued number remains resolvable as an alias.
    """
    clusters = _clusters(groups)
    record_cluster = {
        record_id: index
        for index, cluster in enumerate(clusters)
        for record_id in cluster
    }
    hits: dict[str, tuple[int, ...]] = {}
    for person, records in person_lineages.items():
        touched = tuple(
            sorted(
                {
                    record_cluster[str(record_id)]
                    for record_id in records
                    if str(record_id) in record_cluster
                }
            )
        )
        if touched:
            hits[str(person)] = touched

    def order(person: str) -> tuple[int, str]:
        if person not in person_sequences:
            raise ValueError(f"Missing sequence for Unified Person {person}")
        return int(person_sequences[person]), person

    assignments: dict[int, str] = {}
    used: set[str] = set()
    for index in range(len(clusters)):
        candidates = sorted(
            (person for person, touched in hits.items() if touched == (index,)),
            key=order,
        )
        if candidates:
            assignments[index] = candidates[0]
            used.add(candidates[0])

    remaining = [index for index in range(len(clusters)) if index not in assignments]
    reusable = sorted((person for person in hits if person not in used), key=order)
    if remaining and reusable:
        survivor = reusable[0]
        compatible = [index for index in hits[survivor] if index in remaining]
        target = min(compatible or remaining, key=lambda index: clusters[index])
        assignments[target] = survivor
        used.add(survivor)

    deterministic_survivor = min(
        assignments,
        key=lambda index: (person_sequences[assignments[index]], clusters[index]),
    ) if assignments else 0
    aliases: dict[str, int] = {}
    for person, touched in hits.items():
        if person in used:
            continue
        if len(touched) == 1:
            aliases[person] = touched[0]
        else:
            aliases[person] = deterministic_survivor

    return LineagePlan(
        clusters=clusters,
        cluster_people=assignments,
        alias_cluster=aliases,
    )
