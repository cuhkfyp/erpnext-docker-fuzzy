"""Pure helpers for complete, bounded identity-overlap resolution."""

from __future__ import annotations

from typing import Iterable

from .correction import MAX_CORRECTION_RECORDS, normalize_partition


STRUCTURAL_OVERLAP_CONFLICTS = frozenset(
    {
        "partial_existing_identity_group",
        "conflicting_active_identity_groups",
        "active_human_exclusion",
    }
)


def structural_overlap_only(conflicts: Iterable[str], *, stale: bool = False) -> bool:
    """Return whether a Tiered failure is solely resolvable identity overlap."""
    values = {str(item) for item in conflicts if str(item)}
    return bool(values) and not stale and values <= STRUCTURAL_OVERLAP_CONFLICTS


def constraint_partition(
    record_ids: Iterable[str],
    same_groups: Iterable[Iterable[str]],
    *,
    maximum_records: int = MAX_CORRECTION_RECORDS,
) -> tuple[tuple[str, ...], ...]:
    """Build the transitive Same partition implied by complete groups/scopes."""
    records = tuple(sorted({str(item).strip() for item in record_ids if str(item).strip()}))
    if len(records) < 2:
        raise ValueError("An overlap resolution requires at least two CCD records")
    if len(records) > maximum_records:
        raise ValueError(
            f"An overlap resolution is limited to {maximum_records} records; this scope has {len(records)}"
        )

    parent = {record_id: record_id for record_id in records}

    def find(record_id: str) -> str:
        while parent[record_id] != record_id:
            parent[record_id] = parent[parent[record_id]]
            record_id = parent[record_id]
        return record_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    record_set = set(records)
    for raw_group in same_groups:
        members = tuple(sorted({str(item).strip() for item in raw_group if str(item).strip()}))
        outside = sorted(set(members) - record_set)
        if outside:
            raise ValueError(
                "A Same constraint references records outside the complete scope: "
                + ", ".join(outside)
            )
        if len(members) < 2:
            continue
        anchor = members[0]
        for member in members[1:]:
            union(anchor, member)

    groups: dict[str, list[str]] = {}
    for record_id in records:
        groups.setdefault(find(record_id), []).append(record_id)
    return normalize_partition(records, groups.values(), maximum_records=maximum_records)


def conflicting_different_pairs(
    groups: Iterable[Iterable[str]],
    different_pairs: Iterable[Iterable[str]],
) -> tuple[tuple[str, str], ...]:
    """Return Different constraints contradicted by the proposed Same partition."""
    group_for = {
        str(record_id): index
        for index, group in enumerate(groups)
        for record_id in group
    }
    conflicts = set()
    for raw_pair in different_pairs:
        values = tuple(str(item) for item in raw_pair)
        if len(values) != 2 or values[0] == values[1]:
            raise ValueError("Every Different constraint must contain two distinct CCD records")
        left, right = values
        if left not in group_for or right not in group_for:
            raise ValueError("A Different constraint references records outside the complete scope")
        if group_for[left] == group_for[right]:
            conflicts.add(tuple(sorted((left, right))))
    return tuple(sorted(conflicts))


def partition_contains_group(
    partition: Iterable[Iterable[str]], group: Iterable[str]
) -> bool:
    """Return whether one proposed partition group contains a complete prior group."""
    members = {str(item) for item in group}
    return any(members <= {str(item) for item in candidate} for candidate in partition)


def partition_splits_groups(
    partition: Iterable[Iterable[str]], prior_groups: Iterable[Iterable[str]]
) -> tuple[tuple[str, ...], ...]:
    """Return complete prior Same groups split by a proposed partition."""
    normalized_partition = tuple(tuple(str(item) for item in group) for group in partition)
    normalized_prior = tuple(
        tuple(sorted(str(item) for item in group)) for group in prior_groups
    )
    split = {
        group
        for group in normalized_prior
        if len(group) > 1 and not partition_contains_group(normalized_partition, group)
    }
    return tuple(sorted(split))
