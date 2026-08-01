"""Deterministic stratified review sampling."""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from collections.abc import Iterable

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
    groups: dict[str, list[EvaluationResult]] = defaultdict(list)
    for result in results:
        groups[stratum(result)].append(result)
    queues: list[deque[EvaluationResult]] = []
    for key in sorted(groups):
        ranked = sorted(
            groups[key],
            key=lambda item: hashlib.sha256(
                f"{seed}:{item.pair.left_id}:{item.pair.right_id}".encode()
            ).hexdigest(),
        )
        queues.append(deque(ranked))
    selected: list[EvaluationResult] = []
    while queues and len(selected) < sample_size:
        next_round: list[deque[EvaluationResult]] = []
        for queue in queues:
            if queue and len(selected) < sample_size:
                selected.append(queue.popleft())
            if queue:
                next_round.append(queue)
        queues = next_round
    return selected


def double_review_ids(results: Iterable[EvaluationResult], count: int, *, seed: str) -> set[str]:
    ranked = sorted(
        results,
        key=lambda item: hashlib.sha256(
            f"double:{seed}:{item.pair.left_id}:{item.pair.right_id}".encode()
        ).hexdigest(),
    )
    return {f"{item.pair.left_id}::{item.pair.right_id}" for item in ranked[: max(0, count)]}
