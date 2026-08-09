"""Deterministic stratified review sampling."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
import heapq

from .types import EvaluationResult


def _band(score: float | None) -> str:
    if score is None:
        return "unavailable"
    if score < 0.55:
        return "lt_055"
    if score < 0.65:
        return "055_065"
    if score < 0.75:
        return "065_075"
    if score < 0.85:
        return "075_085"
    return "gte_085"


def stratum(result: EvaluationResult) -> str:
    disagreement = result.baseline.tier != result.tiered_gated.tier
    conflict = result.tiered_gated.tier.value == "conflict_review"
    routes = "+".join(result.pair.blocking_routes)
    return "|".join(
        (
            result.pair.source_pair,
            _band(result.baseline.score),
            "models_disagree" if disagreement else "models_agree",
            "id_conflict" if conflict else "no_id_conflict",
            routes,
        )
    )


def _digest(result: EvaluationResult, seed: str, *, prefix: str = "sample") -> int:
    return int(
        hashlib.sha256(
            f"{prefix}:{seed}:{result.pair.left_id}:{result.pair.right_id}".encode()
        ).hexdigest(),
        16,
    )


def _balanced_rows(
    grouped: dict[str, list[tuple[int, EvaluationResult]]],
) -> Iterable[tuple[int, EvaluationResult]]:
    """Yield deterministic round-robin rows across source pairs."""
    ordered = {
        key: sorted(rows, key=lambda item: item[0])
        for key, rows in sorted(grouped.items())
    }
    offsets = {key: 0 for key in ordered}
    while True:
        emitted = False
        for key, rows in ordered.items():
            offset = offsets[key]
            if offset >= len(rows):
                continue
            emitted = True
            offsets[key] = offset + 1
            yield rows[offset]
        if not emitted:
            return


def balanced_quotas(source_pair_counts: dict[str, int], sample_size: int) -> dict[str, int]:
    """Allocate an equal-as-possible sample without exceeding group capacity."""
    quotas = {key: 0 for key, count in source_pair_counts.items() if count > 0}
    remaining = min(max(0, sample_size), sum(source_pair_counts.values()))
    active = sorted(quotas)
    while remaining and active:
        next_active = []
        for key in active:
            if remaining <= 0:
                break
            if quotas[key] >= source_pair_counts[key]:
                continue
            quotas[key] += 1
            remaining -= 1
            if quotas[key] < source_pair_counts[key]:
                next_active.append(key)
        active = next_active
    return quotas


def stratified_sample(
    results: Iterable[EvaluationResult],
    sample_size: int,
    *,
    seed: str,
    source_pair_counts: dict[str, int] | None = None,
) -> list[EvaluationResult]:
    if sample_size <= 0:
        return []

    # Retain one deterministic representative per observed stratum plus a
    # bottom-k reservoir for each source pair.  Selection then round-robins
    # across source pairs.  This prevents a high-volume source pair from
    # dominating the review set while keeping memory bounded by
    # O(number_of_strata + source_pairs * sample_size), rather than by the full
    # candidate population.
    quotas = balanced_quotas(source_pair_counts or {}, sample_size)
    representatives: dict[str, tuple[int, EvaluationResult]] = {}
    source_reservoirs: dict[str, list[tuple[int, int, EvaluationResult]]] = {}
    sequence = 0
    for result in results:
        digest = _digest(result, seed)
        key = stratum(result)
        current = representatives.get(key)
        if current is None or digest < current[0]:
            representatives[key] = (digest, result)

        reservoir = source_reservoirs.setdefault(result.pair.source_pair, [])
        reservoir_limit = quotas.get(result.pair.source_pair, sample_size)
        if reservoir_limit <= 0:
            continue
        item = (-digest, sequence, result)
        sequence += 1
        if len(reservoir) < reservoir_limit:
            heapq.heappush(reservoir, item)
        elif digest < -reservoir[0][0]:
            heapq.heapreplace(reservoir, item)

    representative_groups: dict[str, list[tuple[int, EvaluationResult]]] = {}
    for digest, result in representatives.values():
        representative_groups.setdefault(result.pair.source_pair, []).append((digest, result))

    selected: list[EvaluationResult] = []
    selected_ids: set[tuple[str, str]] = set()
    selected_by_source: dict[str, int] = {}
    for _, result in _balanced_rows(representative_groups):
        if len(selected) >= sample_size:
            break
        source_pair = result.pair.source_pair
        if quotas and selected_by_source.get(source_pair, 0) >= quotas.get(source_pair, 0):
            continue
        pair_id = (result.pair.left_id, result.pair.right_id)
        selected.append(result)
        selected_ids.add(pair_id)
        selected_by_source[source_pair] = selected_by_source.get(source_pair, 0) + 1

    reservoir_groups = {
        source_pair: [
            (-negative_digest, result)
            for negative_digest, _, result in reservoir
        ]
        for source_pair, reservoir in source_reservoirs.items()
    }
    for _, result in _balanced_rows(reservoir_groups):
        pair_id = (result.pair.left_id, result.pair.right_id)
        if len(selected) >= sample_size:
            break
        source_pair = result.pair.source_pair
        if quotas and selected_by_source.get(source_pair, 0) >= quotas.get(source_pair, 0):
            continue
        if pair_id not in selected_ids:
            selected.append(result)
            selected_ids.add(pair_id)
            selected_by_source[source_pair] = selected_by_source.get(source_pair, 0) + 1
    return selected


def double_review_ids(results: Iterable[EvaluationResult], count: int, *, seed: str) -> set[str]:
    grouped: dict[str, list[tuple[int, EvaluationResult]]] = {}
    for result in results:
        grouped.setdefault(result.pair.source_pair, []).append(
            (_digest(result, seed, prefix="double"), result)
        )
    selected = []
    for _, result in _balanced_rows(grouped):
        if len(selected) >= max(0, count):
            break
        selected.append(result)
    return {f"{item.pair.left_id}::{item.pair.right_id}" for item in selected}
