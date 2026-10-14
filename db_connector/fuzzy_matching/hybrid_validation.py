"""Pure selection helpers for Development-only hybrid prospective validation."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .splink_automation import validation_gate


HYBRID_VALIDATION_SIZE = 165
HYBRID_HOLDOUT_SIZE = 50
HYBRID_MINIMUM_ELIGIBLE_HOLDOUT = 45
HYBRID_MINIMUM_COMBINED_VALID = 195
HYBRID_MINIMUM_SCORE = 0.95
HYBRID_PROPOSAL_ROUTES = (
    "exact_name+unverified_identifier_exact",
    "name_support+unverified_identifier+secondary_exact",
)


def hybrid_holdout_gate(
    validation_labels: Iterable[str],
    holdout_labels: Iterable[str],
    *,
    validation_stale_count: int = 0,
    validation_unresolved_count: int = 0,
    holdout_stale_count: int = 0,
    holdout_unresolved_count: int = 0,
) -> dict[str, Any]:
    """Evaluate the Development-only holdout and combined evidence gates."""
    validation = tuple(str(label) for label in validation_labels)
    holdout = tuple(str(label) for label in holdout_labels)
    holdout_result = validation_gate(
        holdout,
        stale_count=holdout_stale_count,
        unresolved_count=holdout_unresolved_count,
        minimum_valid=HYBRID_MINIMUM_ELIGIBLE_HOLDOUT,
        lower_target=0.0,
    )
    combined = validation_gate(
        validation + holdout,
        stale_count=validation_stale_count + holdout_stale_count,
        unresolved_count=validation_unresolved_count + holdout_unresolved_count,
        minimum_valid=HYBRID_MINIMUM_COMBINED_VALID,
        lower_target=0.95,
    )
    reasons = list(combined["reasons"])
    if holdout_result["valid"] < HYBRID_MINIMUM_ELIGIBLE_HOLDOUT:
        reason = (
            f"insufficient_holdout_pairs:{holdout_result['valid']}:"
            f"{HYBRID_MINIMUM_ELIGIBLE_HOLDOUT}"
        )
        if reason not in reasons:
            reasons.append(reason)
    if holdout_unresolved_count:
        reason = f"unresolved_holdout_pairs:{int(holdout_unresolved_count)}"
        if reason not in reasons:
            reasons.append(reason)
    combined = {**combined, "passed": not reasons, "reasons": tuple(reasons)}
    return {"holdout": holdout_result, "combined": combined}


def _rank(seed: str, cohort: str, value: str) -> tuple[str, str]:
    return (
        hashlib.sha256(f"{seed}\x1f{cohort}\x1f{value}".encode()).hexdigest(),
        value,
    )


def _fill_proportionally(
    allocation: dict[str, int],
    capacities: Mapping[str, int],
    target: int,
) -> None:
    """Fill a capped allocation using largest remainder and stable ties."""
    if sum(allocation.values()) > int(target):
        raise ValueError("The minimum stratum allocation exceeds the cohort target")
    while sum(allocation.values()) < int(target):
        remaining_slots = int(target) - sum(allocation.values())
        residual = {
            source: max(int(capacities[source]) - int(allocation.get(source, 0)), 0)
            for source in sorted(capacities)
        }
        active = [source for source, count in residual.items() if count]
        if not active:
            raise ValueError("The eligible population cannot fill the requested cohort")
        total_residual = sum(residual[source] for source in active)
        raw = {
            source: remaining_slots * residual[source] / total_residual
            for source in active
        }
        grants = {
            source: min(residual[source], int(raw[source])) for source in active
        }
        granted = sum(grants.values())
        for source, count in grants.items():
            allocation[source] = int(allocation.get(source, 0)) + count
        if granted:
            continue
        ranked = sorted(
            active,
            key=lambda source: (-(raw[source] % 1), -residual[source], source),
        )
        for source in ranked:
            if sum(allocation.values()) >= int(target):
                break
            allocation[source] = int(allocation.get(source, 0)) + 1


def dynamic_source_allocations(
    capacities: Mapping[str, int],
    *,
    validation_size: int = HYBRID_VALIDATION_SIZE,
    holdout_size: int = HYBRID_HOLDOUT_SIZE,
) -> dict[str, dict[str, int]]:
    """Allocate every available source to validation and, where possible, holdout."""
    available = {
        str(source): max(int(count), 0)
        for source, count in capacities.items()
        if int(count) > 0
    }
    required = int(validation_size) + int(holdout_size)
    if sum(available.values()) < required:
        raise ValueError(
            f"Only {sum(available.values())} unseen eligible pairs are available; {required} required"
        )
    if len(available) > int(validation_size):
        raise ValueError("There are more active source strata than validation positions")

    validation = {source: 1 for source in sorted(available)}
    holdout = {source: 0 for source in sorted(available)}
    holdout_sources = [
        source for source in sorted(available) if available[source] >= 2
    ]
    if len(holdout_sources) > int(holdout_size):
        holdout_sources = sorted(
            holdout_sources, key=lambda source: (-available[source], source)
        )[: int(holdout_size)]
    for source in holdout_sources:
        holdout[source] = 1

    validation_capacity = {
        source: available[source] - holdout[source] for source in available
    }
    _fill_proportionally(validation, validation_capacity, int(validation_size))
    holdout_capacity = {
        source: available[source] - validation[source] for source in available
    }
    _fill_proportionally(holdout, holdout_capacity, int(holdout_size))
    return {"validation": validation, "holdout": holdout}


def _stratified_names(
    rows: list[dict[str, Any]],
    count: int,
    *,
    seed: str,
    cohort: str,
) -> list[str]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[(str(row["primary_route"]), str(row["score_band"]))].append(row)
    for stratum, members in buckets.items():
        members.sort(
            key=lambda row: _rank(
                seed,
                f"{cohort}:{stratum[0]}:{stratum[1]}",
                str(row["name"]),
            )
        )
    strata = sorted(
        buckets,
        key=lambda stratum: _rank(seed, f"{cohort}:stratum", "\x1f".join(stratum)),
    )
    selected: list[str] = []
    offsets = {stratum: 0 for stratum in strata}
    while len(selected) < int(count):
        progressed = False
        for stratum in strata:
            offset = offsets[stratum]
            members = buckets[stratum]
            if offset >= len(members):
                continue
            selected.append(str(members[offset]["name"]))
            offsets[stratum] += 1
            progressed = True
            if len(selected) >= int(count):
                break
        if not progressed:
            raise ValueError("A source stratum could not fill its frozen allocation")
    return selected


def select_dynamic_hybrid_cohorts(
    rows: Iterable[Mapping[str, Any]],
    *,
    seed: str,
    validation_size: int = HYBRID_VALIDATION_SIZE,
    holdout_size: int = HYBRID_HOLDOUT_SIZE,
) -> dict[str, Any]:
    """Select disjoint source/route/score-stratified cohorts deterministically."""
    materialized = [dict(row) for row in rows]
    names = [str(row.get("name") or "") for row in materialized]
    if not str(seed or "").strip():
        raise ValueError("A deterministic validation seed is required")
    if not all(names) or len(names) != len(set(names)):
        raise ValueError("Every eligible hybrid pair requires a unique name")
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in materialized:
        source = str(row.get("source_pair") or "").strip()
        if not source:
            raise ValueError("Every eligible hybrid pair requires a source pair")
        if not row.get("primary_route") or not row.get("score_band"):
            raise ValueError("Every eligible hybrid pair requires frozen research strata")
        by_source[source].append(row)

    allocations = dynamic_source_allocations(
        {source: len(members) for source, members in by_source.items()},
        validation_size=validation_size,
        holdout_size=holdout_size,
    )
    validation: list[str] = []
    holdout: list[str] = []
    for source in sorted(by_source):
        source_rows = by_source[source]
        chosen_validation = _stratified_names(
            source_rows,
            allocations["validation"][source],
            seed=seed,
            cohort=f"validation:{source}",
        )
        chosen_set = set(chosen_validation)
        chosen_holdout = _stratified_names(
            [row for row in source_rows if str(row["name"]) not in chosen_set],
            allocations["holdout"][source],
            seed=seed,
            cohort=f"holdout:{source}",
        )
        validation.extend(chosen_validation)
        holdout.extend(chosen_holdout)

    validation.sort(key=lambda name: _rank(seed, "validation:final", name))
    holdout.sort(key=lambda name: _rank(seed, "holdout:final", name))
    if len(validation) != int(validation_size) or len(holdout) != int(holdout_size):
        raise ValueError("The dynamic hybrid allocation produced the wrong cohort size")
    if set(validation) & set(holdout):
        raise ValueError("Hybrid validation and holdout cohorts overlap")
    return {
        "validation": tuple(validation),
        "holdout": tuple(holdout),
        "source_allocations": allocations,
    }
