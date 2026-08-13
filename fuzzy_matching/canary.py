"""Pure safety gates for reversible full-population matching recommendations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


def ordered_pair(left_id: Any, right_id: Any) -> tuple[str, str]:
    return tuple(sorted((str(left_id), str(right_id))))


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


@dataclass(frozen=True)
class CanaryEdge:
    left_id: str
    right_id: str
    left_source: str
    right_source: str
    source_pair: str
    blocking_routes: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    approved_rule: bool = True

    @property
    def pair_key(self) -> tuple[str, str]:
        return ordered_pair(self.left_id, self.right_id)


@dataclass(frozen=True)
class GateDecision:
    status: str
    reasons: tuple[str, ...]
    cluster_fingerprint: str
    cluster_size: int


def _cluster_fingerprint(record_ids: Iterable[str]) -> str:
    payload = "\x1f".join(sorted(set(record_ids)))
    return hashlib.sha256(payload.encode()).hexdigest()


def canonical_identity_groups(
    record_ids: Iterable[str],
    same_pairs: Iterable[tuple[str, str]] = (),
) -> tuple[tuple[str, ...], ...]:
    """Return a stable identity partition implied by selected Same pairs.

    The review UI lets a reviewer choose pairs that belong together.  Connected
    selections are deliberately closed transitively: choosing A=B and B=C means
    the stored decision is the single group A=B=C.  Unselected records remain
    singleton groups and no CCD record is modified.
    """
    ordered_ids = tuple(sorted({str(item) for item in record_ids if str(item)}))
    if not ordered_ids:
        raise ValueError("At least one record is required")
    allowed = set(ordered_ids)
    graph = _UnionFind()
    for item in ordered_ids:
        graph.find(item)
    for left, right in same_pairs:
        pair = ordered_pair(left, right)
        if pair[0] not in allowed or pair[1] not in allowed:
            raise ValueError("A selected pair is outside the reviewed component")
        if pair[0] == pair[1]:
            raise ValueError("A record cannot be paired with itself")
        graph.union(*pair)
    groups: dict[str, list[str]] = defaultdict(list)
    for item in ordered_ids:
        groups[graph.find(item)].append(item)
    return tuple(sorted(tuple(sorted(group)) for group in groups.values()))


def identity_partition_fingerprint(groups: Iterable[Iterable[str]]) -> str:
    """Hash a canonical human decision without exposing record identifiers."""
    canonical = tuple(sorted(tuple(sorted(str(item) for item in group)) for group in groups))
    payload = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def analyze_canary_edges(
    edges: Iterable[CanaryEdge],
    records: dict[str, dict[str, Any]],
    *,
    validated_source_pairs: set[str],
    conflicting_pairs: set[tuple[str, str]] | None = None,
    stale_record_ids: set[str] | None = None,
) -> dict[tuple[str, str], GateDecision]:
    """Classify deterministic High edges without creating identity clusters.

    ``records`` contains only non-identifying gate metadata: ``source`` and a
    mapping of normalized complete ``trusted_ids``. The returned cluster hash
    is an opaque correlation key, not a person identifier.
    """
    rows = list(edges)
    conflicts = {ordered_pair(*pair) for pair in (conflicting_pairs or set())}
    stale = {str(item) for item in (stale_record_ids or set())}
    graph = _UnionFind()
    for edge in rows:
        graph.union(edge.left_id, edge.right_id)

    nodes_by_root: dict[str, set[str]] = defaultdict(set)
    edges_by_root: dict[str, list[CanaryEdge]] = defaultdict(list)
    for edge in rows:
        root = graph.find(edge.left_id)
        nodes_by_root[root].update((edge.left_id, edge.right_id))
        edges_by_root[root].append(edge)

    component_reasons: dict[str, set[str]] = defaultdict(set)
    for root, node_ids in nodes_by_root.items():
        source_counts = Counter(str((records.get(item) or {}).get("source") or "") for item in node_ids)
        if "" in source_counts:
            component_reasons[root].add("missing_governed_source")
        if any(count > 1 for source, count in source_counts.items() if source):
            component_reasons[root].add("one_to_many_source_conflict")

        trusted_values: dict[str, set[str]] = defaultdict(set)
        for item in node_ids:
            for attribute, value in dict(
                (records.get(item) or {}).get("trusted_ids") or {}
            ).items():
                if value:
                    trusted_values[str(attribute)].add(str(value))
        for attribute, values in trusted_values.items():
            if len(values) > 1:
                component_reasons[root].add(
                    f"transitive_trusted_identifier_conflict:{attribute}"
                )

        if any(
            left in node_ids and right in node_ids
            for left, right in conflicts
        ):
            component_reasons[root].add("transitive_model_conflict")

    decisions: dict[tuple[str, str], GateDecision] = {}
    for root, component_edges in edges_by_root.items():
        node_ids = nodes_by_root[root]
        component = component_reasons[root]
        fingerprint = _cluster_fingerprint(node_ids)
        for edge in component_edges:
            reasons = set(component)
            if not edge.approved_rule:
                reasons.add("unvalidated_high_rule")
            if edge.source_pair not in validated_source_pairs:
                reasons.add("unvalidated_source_pair")
            if edge.left_id in stale or edge.right_id in stale:
                reasons.add("stale_record")
            decisions[edge.pair_key] = GateDecision(
                status="Exception" if reasons else "Proposed",
                reasons=tuple(sorted(reasons)),
                cluster_fingerprint=fingerprint,
                cluster_size=len(node_ids),
            )
    return decisions
