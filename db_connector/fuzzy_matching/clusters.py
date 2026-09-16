"""Detect inconsistent connected groups without creating person clusters."""

from __future__ import annotations

from collections.abc import Iterable

from .types import EvaluationResult, MatchTier


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left


def inconsistent_pairs(results: Iterable[EvaluationResult]) -> set[tuple[str, str]]:
    rows = list(results)
    graph = _UnionFind()
    for result in rows:
        if result.tiered_gated.tier in {MatchTier.HIGH, MatchTier.REVIEW}:
            graph.union(result.pair.left_id, result.pair.right_id)
    conflicted_roots = set()
    for result in rows:
        if result.tiered_gated.tier == MatchTier.CONFLICT:
            left_root = graph.find(result.pair.left_id)
            right_root = graph.find(result.pair.right_id)
            if left_root == right_root:
                conflicted_roots.add(left_root)
    output = set()
    for result in rows:
        if graph.find(result.pair.left_id) in conflicted_roots:
            output.add(tuple(sorted((result.pair.left_id, result.pair.right_id))))
    return output
