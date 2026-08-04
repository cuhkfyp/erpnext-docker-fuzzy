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


def stratified_sample(
    results: Iterable[EvaluationResult],
    sample_size: int,
    *,
    seed: str,
) -> list[EvaluationResult]:
    if sample_size <= 0:
        return []

    # Retain one deterministic representative per observed stratum, plus one
    # global hash reservoir used to fill the remaining slots. This bounds rich
    # EvaluationResult objects to O(number_of_strata + sample_size) rather than
    # materialising every candidate pair (up to hundreds of thousands).
    representatives: dict[str, tuple[int, EvaluationResult]] = {}
    reservoir: list[tuple[int, int, EvaluationResult]] = []
    sequence = 0
    for result in results:
        digest = int(
            hashlib.sha256(
                f"{seed}:{result.pair.left_id}:{result.pair.right_id}".encode()
            ).hexdigest(),
            16,
        )
        key = stratum(result)
        current = representatives.get(key)
        if current is None or digest < current[0]:
            representatives[key] = (digest, result)

        item = (-digest, sequence, result)
        sequence += 1
        if len(reservoir) < sample_size:
            heapq.heappush(reservoir, item)
        elif digest < -reservoir[0][0]:
            heapq.heapreplace(reservoir, item)

    representative_rows = sorted(representatives.values(), key=lambda item: item[0])
    selected = [result for _, result in representative_rows[:sample_size]]
    selected_ids = {
        (result.pair.left_id, result.pair.right_id)
        for result in selected
    }
    global_rows = sorted(
        ((-negative_digest, result) for negative_digest, _, result in reservoir),
        key=lambda item: item[0],
    )
    for _, result in global_rows:
        pair_id = (result.pair.left_id, result.pair.right_id)
        if len(selected) >= sample_size:
            break
        if pair_id not in selected_ids:
            selected.append(result)
            selected_ids.add(pair_id)
    return selected


def double_review_ids(results: Iterable[EvaluationResult], count: int, *, seed: str) -> set[str]:
    ranked = sorted(
        results,
        key=lambda item: hashlib.sha256(
            f"double:{seed}:{item.pair.left_id}:{item.pair.right_id}".encode()
        ).hexdigest(),
    )
    return {f"{item.pair.left_id}::{item.pair.right_id}" for item in ranked[: max(0, count)]}
