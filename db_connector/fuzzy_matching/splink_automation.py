"""Pure governance helpers for guarded Splink automation.

The functions in this module deliberately have no Frappe dependency.  They are
used by the Desk/background-job service and can be tested without a site.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .metrics import wilson_interval


FROZEN_AUTOMATIC_CUTOFF = 0.999148140
VALIDATION_SIZE = 165
HOLDOUT_SIZE = 50
MIN_VALID_VALIDATION = 150
MIN_ELIGIBLE_HOLDOUT = 45
PRECISION_LOWER_TARGET = 0.95
WEEKLY_PAIR_CAPACITY = 20

VALIDATION_SOURCE_ALLOCATION = {
    "DHCE::HMSSHP": 136,
    "HKSReCCMS::SHP-UAT": 20,
    "HMSSHP::SHP-UAT": 8,
    "PHI-UAT::SHP-UAT": 1,
}
HOLDOUT_SOURCE_ALLOCATION = {
    "DHCE::HMSSHP": 41,
    "HKSReCCMS::SHP-UAT": 6,
    "HMSSHP::SHP-UAT": 3,
    "PHI-UAT::SHP-UAT": 0,
}

# Queue candidates retain the exact CCD Registration source identifiers.  The
# prospective study protocol uses stable, human-readable governance labels;
# keep that translation explicit so a new source identifier cannot silently
# enter an approved stratum.
GOVERNED_SOURCE_LABELS = {
    "HQ-vDB01_DHCE_Prod": "DHCE",
    "HQ-vDB01_HKSReCCMS_PROD": "HKSReCCMS",
    "HQ-vDB01_HMSSHP_Prod": "HMSSHP",
    "PHI-vDBUAT_HMSPhi_UAT": "PHI-UAT",
    "SHP-DB-UAT_HMSSHP_UAT": "SHP-UAT",
}


def ordered_pair(left: Any, right: Any) -> tuple[str, str]:
    return tuple(sorted((str(left), str(right))))  # type: ignore[return-value]


def canonical_source_pair(value: Any) -> str:
    """Normalize display separators while retaining the governed source names."""
    text = str(value or "").strip()
    for separator in ("↔", "<->", "::", " / ", "|"):
        if separator in text:
            parts = [part.strip() for part in text.split(separator)]
            if len(parts) == 2 and all(parts):
                return "::".join(
                    sorted(GOVERNED_SOURCE_LABELS.get(part, part) for part in parts)
                )
    return text


def _stable_rank(seed: str, cohort: str, row: Mapping[str, Any]) -> tuple[str, str]:
    identity = str(
        row.get("pair_fingerprint")
        or row.get("pair_key")
        or row.get("name")
        or "\x1f".join(ordered_pair(row.get("left_record"), row.get("right_record")))
    )
    return (
        hashlib.sha256(f"{seed}\x1f{cohort}\x1f{identity}".encode()).hexdigest(),
        str(row.get("name") or identity),
    )


def component_metadata(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return deterministic connected-component and clique metadata per edge.

    Duplicate orientations of an edge are treated as one graph edge.  A
    component is complete only when every possible pair is represented.
    """
    materialized = [dict(row) for row in rows]
    adjacency: dict[str, set[str]] = defaultdict(set)
    edges: set[tuple[str, str]] = set()
    for row in materialized:
        left, right = ordered_pair(row.get("left_record"), row.get("right_record"))
        if not left or not right or left == right:
            continue
        adjacency[left].add(right)
        adjacency[right].add(left)
        edges.add((left, right))

    record_component: dict[str, tuple[str, ...]] = {}
    for start in sorted(adjacency):
        if start in record_component:
            continue
        pending = [start]
        members: set[str] = set()
        while pending:
            record = pending.pop()
            if record in members:
                continue
            members.add(record)
            pending.extend(sorted(adjacency[record] - members, reverse=True))
        component = tuple(sorted(members))
        for record in component:
            record_component[record] = component

    result: dict[str, dict[str, Any]] = {}
    for row in materialized:
        left, right = ordered_pair(row.get("left_record"), row.get("right_record"))
        component = record_component.get(left, (left, right))
        component_edges = {
            edge for edge in edges if edge[0] in component and edge[1] in component
        }
        expected_edges = len(component) * (len(component) - 1) // 2
        fingerprint = hashlib.sha256("\x1f".join(component).encode()).hexdigest()
        key = str(row.get("name") or row.get("pair_key") or f"{left}\x1f{right}")
        result[key] = {
            "component_fingerprint": fingerprint,
            "component_records": component,
            "component_size": len(component),
            "component_edge_count": len(component_edges),
            "complete_clique": len(component_edges) == expected_edges,
            "isolated_pair": len(component) == 2 and len(component_edges) == 1,
        }
    return result


def select_stratified_cohorts(
    rows: Iterable[Mapping[str, Any]],
    *,
    seed: str,
    validation_allocation: Mapping[str, int] = VALIDATION_SOURCE_ALLOCATION,
    holdout_allocation: Mapping[str, int] = HOLDOUT_SOURCE_ALLOCATION,
) -> dict[str, tuple[str, ...]]:
    """Select disjoint, reproducible source-stratified validation cohorts."""
    if not str(seed or "").strip():
        raise ValueError("A non-empty deterministic cohort seed is required")
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_names: set[str] = set()
    for item in rows:
        row = dict(item)
        name = str(row.get("name") or "")
        if not name or name in seen_names:
            raise ValueError("Every cohort candidate requires a unique name")
        seen_names.add(name)
        if row.get("known_label") or row.get("final_label"):
            continue
        buckets[canonical_source_pair(row.get("source_pair"))].append(row)

    validation: list[str] = []
    holdout: list[str] = []
    all_sources = sorted(set(validation_allocation) | set(holdout_allocation))
    for source_pair in all_sources:
        validation_count = int(validation_allocation.get(source_pair, 0))
        holdout_count = int(holdout_allocation.get(source_pair, 0))
        available = buckets.get(canonical_source_pair(source_pair), [])
        ranked = sorted(available, key=lambda row: _stable_rank(seed, "all", row))
        required = validation_count + holdout_count
        if len(ranked) < required:
            raise ValueError(
                f"Source pair {source_pair} has {len(ranked)} eligible pairs; {required} required"
            )
        # A single frozen permutation avoids independently sampled cohorts and
        # therefore guarantees disjointness without order-sensitive retries.
        validation.extend(str(row["name"]) for row in ranked[:validation_count])
        holdout.extend(
            str(row["name"])
            for row in ranked[validation_count : validation_count + holdout_count]
        )

    if len(validation) != sum(int(value) for value in validation_allocation.values()):
        raise ValueError("Validation allocation did not produce its exact frozen size")
    if len(holdout) != sum(int(value) for value in holdout_allocation.values()):
        raise ValueError("Holdout allocation did not produce its exact frozen size")
    if set(validation) & set(holdout):
        raise ValueError("Validation and holdout cohorts overlap")
    return {"validation": tuple(validation), "holdout": tuple(holdout)}


def validation_gate(
    labels: Sequence[str],
    *,
    stale_count: int = 0,
    unresolved_count: int = 0,
    minimum_valid: int = MIN_VALID_VALIDATION,
    lower_target: float = PRECISION_LOWER_TARGET,
) -> dict[str, Any]:
    """Evaluate the frozen prospective-validation authorization gate."""
    same = sum(label == "Same" for label in labels)
    different = sum(label == "Different" for label in labels)
    valid = same + different
    lower, upper = wilson_interval(same, valid)
    reasons: list[str] = []
    if unresolved_count:
        reasons.append(f"unresolved_pairs:{int(unresolved_count)}")
    if valid < int(minimum_valid):
        reasons.append(f"insufficient_valid_pairs:{valid}:{int(minimum_valid)}")
    if lower < float(lower_target):
        reasons.append(f"wilson_lower_below_target:{lower:.9f}:{float(lower_target):.9f}")
    return {
        "valid": valid,
        "same": same,
        "different": different,
        "precision": same / valid if valid else 0.0,
        "wilson_95": (lower, upper),
        "passed": not reasons,
        "reasons": tuple(reasons),
    }


def provenance_fingerprint(values: Mapping[str, Any]) -> str:
    required = (
        "queue_run",
        "canary_run",
        "policy_snapshot_sha256",
        "model_versions_json",
        "runtime_versions_json",
        "source_scope_json",
        "frozen_cutoff",
    )
    missing = [name for name in required if values.get(name) in (None, "")]
    if missing:
        raise ValueError("Missing frozen provenance: " + ", ".join(missing))
    canonical = "\x1e".join(f"{name}={values[name]}" for name in required)
    return hashlib.sha256(canonical.encode()).hexdigest()


def authorization_staleness(
    frozen: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return full-authorization invalidators, excluding record-level drift."""
    governed = (
        "queue_run",
        "canary_run",
        "policy_snapshot_sha256",
        "model_versions_json",
        "runtime_versions_json",
        "source_scope_json",
        "frozen_cutoff",
    )
    return tuple(name for name in governed if str(frozen.get(name)) != str(current.get(name)))


def allocate_shared_capacity(
    mandatory_volume: Mapping[str, int],
    *,
    capacity: int = WEEKLY_PAIR_CAPACITY,
    minimum_per_active_channel: int = 2,
    recent_unattended_volume: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Allocate bounded pair cases, with a floor then proportional remainder."""
    remaining_need = {
        str(channel): max(int(count), 0)
        for channel, count in mandatory_volume.items()
        if int(count) > 0
    }
    allocation = {str(channel): 0 for channel in mandatory_volume}
    remaining = max(int(capacity), 0)

    for channel in sorted(remaining_need):
        granted = min(remaining_need[channel], int(minimum_per_active_channel), remaining)
        allocation[channel] = granted
        remaining_need[channel] -= granted
        remaining -= granted
        if not remaining:
            return allocation

    while remaining and any(remaining_need.values()):
        active = [channel for channel, need in remaining_need.items() if need]
        weights = {
            channel: max(int((recent_unattended_volume or {}).get(channel, 0)), 0)
            for channel in active
        }
        if not sum(weights.values()):
            weights = {channel: remaining_need[channel] for channel in active}
        total_weight = sum(weights.values())
        quotas = {
            channel: remaining * weights[channel] / total_weight
            for channel in active
        }
        grants = {
            channel: min(remaining_need[channel], int(math.floor(quota)))
            for channel, quota in quotas.items()
        }
        granted_total = sum(grants.values())
        for channel, grant in grants.items():
            allocation[channel] += grant
            remaining_need[channel] -= grant
        remaining -= granted_total
        if not remaining or not any(remaining_need.values()):
            break
        # Largest remainder with a stable channel-name tiebreaker.
        ranked = sorted(
            (channel for channel, need in remaining_need.items() if need),
            key=lambda channel: (-(quotas.get(channel, 0.0) % 1), channel),
        )
        for channel in ranked:
            if not remaining:
                break
            allocation[channel] += 1
            remaining_need[channel] -= 1
            remaining -= 1
    return allocation


@dataclass(frozen=True)
class AutomaticComponent:
    fingerprint: str
    records: tuple[str, ...]
    candidate_names: tuple[str, ...]
    complete_clique: bool


def automatic_components(
    rows: Iterable[Mapping[str, Any]], *, maximum_size: int
) -> tuple[AutomaticComponent, ...]:
    """Return only complete high-score components within the configured bound."""
    materialized = [dict(row) for row in rows]
    metadata = component_metadata(materialized)
    grouped: dict[str, dict[str, Any]] = {}
    for row in materialized:
        key = str(row.get("name") or row.get("pair_key") or "")
        meta = metadata[key]
        group = grouped.setdefault(
            meta["component_fingerprint"],
            {"records": meta["component_records"], "names": [], "complete": meta["complete_clique"]},
        )
        group["names"].append(str(row.get("name") or ""))
    return tuple(
        AutomaticComponent(
            fingerprint=fingerprint,
            records=tuple(group["records"]),
            candidate_names=tuple(sorted(group["names"])),
            complete_clique=bool(group["complete"]),
        )
        for fingerprint, group in sorted(grouped.items())
        if bool(group["complete"]) and 2 <= len(group["records"]) <= int(maximum_size)
    )
