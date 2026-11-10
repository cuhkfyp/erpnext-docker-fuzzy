"""Pure safety gates for reversible full-population matching recommendations."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


ONE_TO_MANY_REASON = "one_to_many_source_conflict"
TRUSTED_IDENTIFIER_CONFLICT_PREFIX = "transitive_trusted_identifier_conflict:"
NAME_CORROBORATED_HIGH_REASON = "exact_name_plus_independent_evidence"
TRUSTED_IDENTIFIER_EXACT_PREFIX = "trusted_global_id_exact:"


def approved_high_rule(
    reasons: Iterable[str], trusted_identifiers: Iterable[str]
) -> bool:
    """Return whether a High result uses a frozen, validated deterministic route.

    The policy has two independent High routes: exact full name plus independent
    evidence, or an exact complete identifier that both sources govern as global.
    A name-conflict warning does not cancel the latter route; contradictory
    complete identifiers are still rejected later by the component safety gate.
    """
    reason_set = {str(reason) for reason in reasons}
    if NAME_CORROBORATED_HIGH_REASON in reason_set:
        return True
    return any(
        f"{TRUSTED_IDENTIFIER_EXACT_PREFIX}{attribute}" in reason_set
        for attribute in {str(value) for value in trusted_identifiers}
    )


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


def summarize_one_to_many_research(
    recommendations: Iterable[dict[str, Any]],
    *,
    reviews: dict[str, dict[str, Any]] | None = None,
    maximum_component_size: int = 3,
) -> dict[str, Any]:
    """Summarize frozen Tiered one-to-many exceptions without changing state.

    A one-to-many exception contains at least three records, so a maximum of
    three means "three-record components only". Missing or uncertain birthday
    and identifier values are deliberately not gates here. Only an already
    recorded conflict between complete normalized trusted identifiers is a hard
    conflict in this research view.
    """
    maximum = int(maximum_component_size or 3)
    if maximum < 3 or maximum > 100:
        raise ValueError("Maximum component size must be between 3 and 100")

    review_rows = reviews or {}
    components: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in recommendations:
        row = dict(raw)
        safety = tuple(str(item) for item in (row.get("safety_reasons") or ()))
        if ONE_TO_MANY_REASON not in safety:
            continue
        fingerprint = str(row.get("cluster_fingerprint") or "")
        if fingerprint:
            components[fingerprint].append(row)

    size_counts: Counter[int] = Counter()
    within_size_counts: Counter[int] = Counter()
    source_patterns: Counter[str] = Counter()
    evidence_bands: Counter[str] = Counter()
    total_edges = 0
    total_records: set[str] = set()
    within_edges = 0
    within_records: set[str] = set()
    hard_conflicts = 0
    reviewed = 0
    applied = 0
    research_candidates = 0
    examples: list[dict[str, Any]] = []

    for fingerprint in sorted(components):
        rows = components[fingerprint]
        records: set[str] = set()
        sources: Counter[str] = Counter()
        safety: set[str] = set()
        edge_reason_sets: list[set[str]] = []
        review_name = ""
        declared_size = 0
        for row in rows:
            declared_size = max(declared_size, int(row.get("cluster_size") or 0))
            for side in ("left", "right"):
                record = str(row.get(f"{side}_record") or "")
                if record:
                    records.add(record)
            safety.update(str(item) for item in (row.get("safety_reasons") or ()))
            edge_reason_sets.append(
                {str(item) for item in (row.get("reason_codes") or ())}
            )
            review_name = review_name or str(row.get("component_review") or "")

        record_sources: dict[str, str] = {}
        for row in rows:
            for side in ("left", "right"):
                record = str(row.get(f"{side}_record") or "")
                source = str(row.get(f"{side}_source") or "")
                if record and source:
                    record_sources[record] = source
        sources.update(record_sources.values())
        size = max(declared_size, len(records))
        size_counts[size] += 1
        total_edges += len(rows)
        total_records.update(records)
        source_pattern = " + ".join(
            f"{source}:{count}" for source, count in sorted(sources.items())
        )
        source_patterns[source_pattern or "unknown"] += 1

        has_trusted_conflict = any(
            reason.startswith(TRUSTED_IDENTIFIER_CONFLICT_PREFIX)
            for reason in safety
        )
        if has_trusted_conflict:
            hard_conflicts += 1

        review = dict(review_rows.get(review_name) or {})
        review_status = str(review.get("review_status") or "Unreviewed")
        materialization_status = str(
            review.get("materialization_status") or "Not Final"
        )
        if review_status in {"Agreed", "Adjudicated"}:
            reviewed += 1
        if materialization_status in {"Applied", "Corrected"}:
            applied += 1

        within_limit = size <= maximum
        if within_limit:
            within_size_counts[size] += 1
            within_edges += len(rows)
            within_records.update(records)

            if edge_reason_sets and all(
                "trusted_global_id_exact" in reasons for reasons in edge_reason_sets
            ):
                evidence_band = "trusted_identifier_exact_on_every_generated_edge"
            elif edge_reason_sets and all(
                "exact_name_plus_independent_evidence" in reasons
                for reasons in edge_reason_sets
            ):
                evidence_band = "approved_name_route_on_every_generated_edge"
            else:
                evidence_band = "mixed_or_incomplete_component_evidence"
            evidence_bands[evidence_band] += 1

            is_candidate = (
                not has_trusted_conflict
                and review_status not in {"Agreed", "Adjudicated", "Stale"}
                and materialization_status not in {
                    "Applied",
                    "Corrected",
                    "Superseded",
                    "Stale",
                }
            )
            if is_candidate:
                research_candidates += 1
            if len(examples) < 12:
                examples.append(
                    {
                        "component_review": review_name,
                        "component_size": size,
                        "edge_count": len(rows),
                        "source_pattern": source_pattern or "unknown",
                        "hard_trusted_identifier_conflict": has_trusted_conflict,
                        "review_status": review_status,
                        "materialization_status": materialization_status,
                        "research_candidate": is_candidate,
                    }
                )

    return {
        "maximum_component_size": maximum,
        "total_component_count": len(components),
        "total_edge_count": total_edges,
        "total_record_count": len(total_records),
        "component_size_counts": dict(sorted(size_counts.items())),
        "within_limit_component_count": sum(within_size_counts.values()),
        "within_limit_edge_count": within_edges,
        "within_limit_record_count": len(within_records),
        "within_limit_size_counts": dict(sorted(within_size_counts.items())),
        "above_limit_component_count": len(components) - sum(within_size_counts.values()),
        "hard_trusted_identifier_conflict_count": hard_conflicts,
        "final_reviewed_component_count": reviewed,
        "applied_component_count": applied,
        "unreviewed_research_candidate_count": research_candidates,
        "source_pattern_counts": dict(
            sorted(source_patterns.items(), key=lambda item: (-item[1], item[0]))
        ),
        "within_limit_evidence_band_counts": dict(sorted(evidence_bands.items())),
        "examples": examples,
        "neutral_missing_or_uncertain_fields": ["birthday", "hkid"],
        "zero_write": True,
    }
