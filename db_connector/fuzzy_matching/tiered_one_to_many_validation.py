"""Pure governance helpers for Tiered one-to-many component automation."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from .metrics import wilson_interval


VALIDATION_SIZE = 165
HOLDOUT_SIZE = 50
MINIMUM_VALIDATION_SIZE = 150
MINIMUM_ELIGIBLE_HOLDOUT = 45
PRECISION_LOWER_TARGET = 0.95
SEED_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_seed(seed: str) -> str:
    value = str(seed or "").strip().casefold()
    if not SEED_PATTERN.fullmatch(value):
        raise ValueError("The governance seed must be exactly 64 hexadecimal characters")
    return value


def _rank(seed: str, cohort: str, value: str) -> tuple[str, str]:
    return (
        hashlib.sha256(f"{seed}\x1f{cohort}\x1f{value}".encode()).hexdigest(),
        str(value),
    )


def component_stratum(row: dict[str, Any]) -> str:
    return f"size={int(row.get('component_size') or 0)}|sources={str(row.get('source_pattern') or '')}"


def _round_robin_names(
    rows: list[dict[str, Any]],
    count: int,
    *,
    seed: str,
    cohort: str,
) -> list[str]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[component_stratum(row)].append(row)
    for stratum, members in buckets.items():
        members.sort(
            key=lambda row: _rank(
                seed,
                f"{cohort}:{stratum}",
                str(row["name"]),
            )
        )
    strata = sorted(
        buckets,
        key=lambda stratum: _rank(seed, f"{cohort}:stratum", stratum),
    )
    offsets = {stratum: 0 for stratum in strata}
    selected: list[str] = []
    while len(selected) < int(count):
        progressed = False
        for stratum in strata:
            offset = offsets[stratum]
            if offset >= len(buckets[stratum]):
                continue
            selected.append(str(buckets[stratum][offset]["name"]))
            offsets[stratum] += 1
            progressed = True
            if len(selected) >= int(count):
                break
        if not progressed:
            raise ValueError("The eligible component population cannot fill the requested cohort")
    return selected


def select_component_cohorts(
    rows: Iterable[dict[str, Any]],
    *,
    seed: str,
    validation_size: int = VALIDATION_SIZE,
    holdout_size: int = HOLDOUT_SIZE,
) -> dict[str, Any]:
    """Return exact, disjoint and order-independent component cohorts.

    Round-robin traversal across the frozen component-size/source-pattern
    strata prevents a large source from silently consuming the cohort. Hash
    ranking within each stratum makes the selection reproducible without
    exposing a manager-facing "try another seed" optimization loop.
    """
    frozen_seed = validate_seed(seed)
    population = [dict(row) for row in rows]
    names = [str(row.get("name") or "") for row in population]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("Every eligible component must have one unique name")
    required = int(validation_size) + int(holdout_size)
    if len(population) < required:
        raise ValueError(
            f"Only {len(population)} eligible components are available; {required} required"
        )

    validation = _round_robin_names(
        population,
        int(validation_size),
        seed=frozen_seed,
        cohort="validation",
    )
    validation_set = set(validation)
    remaining = [row for row in population if str(row["name"]) not in validation_set]
    holdout = _round_robin_names(
        remaining,
        int(holdout_size),
        seed=frozen_seed,
        cohort="holdout",
    )
    by_name = {str(row["name"]): row for row in population}

    def allocation(selected: Iterable[str]) -> dict[str, int]:
        return dict(
            sorted(
                Counter(component_stratum(by_name[name]) for name in selected).items()
            )
        )

    return {
        "validation": tuple(validation),
        "holdout": tuple(holdout),
        "validation_allocation": allocation(validation),
        "holdout_allocation": allocation(holdout),
    }


def component_precision_gate(
    decisions: Iterable[str],
    *,
    stale_count: int = 0,
    unresolved_count: int = 0,
    minimum_valid: int = MINIMUM_VALIDATION_SIZE,
    lower_target: float = PRECISION_LOWER_TARGET,
) -> dict[str, Any]:
    labels = tuple(str(decision or "") for decision in decisions)
    valid = len(labels)
    same = sum(label == "All Same" for label in labels)
    non_same = valid - same
    lower, upper = wilson_interval(same, valid)
    reasons: list[str] = []
    if valid < int(minimum_valid):
        reasons.append(f"insufficient_valid_components:{valid}:{int(minimum_valid)}")
    if int(unresolved_count):
        reasons.append(f"unresolved_components:{int(unresolved_count)}")
    if lower < float(lower_target):
        reasons.append(f"wilson_lower_below_target:{lower:.6f}:{float(lower_target):.6f}")
    return {
        "valid": valid,
        "all_same": same,
        "non_all_same": non_same,
        "precision": same / valid if valid else 0.0,
        "wilson_95": (lower, upper),
        "passed": not reasons,
        "reasons": tuple(reasons),
    }


def holdout_and_combined_gate(
    validation_decisions: Iterable[str],
    holdout_decisions: Iterable[str],
    *,
    validation_stale_count: int = 0,
    holdout_stale_count: int = 0,
    validation_unresolved_count: int = 0,
    holdout_unresolved_count: int = 0,
) -> dict[str, Any]:
    validation = tuple(validation_decisions)
    holdout = tuple(holdout_decisions)
    holdout_gate = component_precision_gate(
        holdout,
        stale_count=holdout_stale_count,
        unresolved_count=holdout_unresolved_count,
        minimum_valid=MINIMUM_ELIGIBLE_HOLDOUT,
        lower_target=0.0,
    )
    combined = component_precision_gate(
        validation + holdout,
        stale_count=validation_stale_count + holdout_stale_count,
        unresolved_count=validation_unresolved_count + holdout_unresolved_count,
        minimum_valid=MINIMUM_VALIDATION_SIZE + MINIMUM_ELIGIBLE_HOLDOUT,
        lower_target=PRECISION_LOWER_TARGET,
    )
    reasons = list(combined["reasons"])
    if holdout_gate["valid"] < MINIMUM_ELIGIBLE_HOLDOUT:
        reason = (
            f"insufficient_holdout_components:{holdout_gate['valid']}:"
            f"{MINIMUM_ELIGIBLE_HOLDOUT}"
        )
        if reason not in reasons:
            reasons.append(reason)
    if holdout_unresolved_count:
        reason = f"unresolved_holdout_components:{int(holdout_unresolved_count)}"
        if reason not in reasons:
            reasons.append(reason)
    return {
        "holdout": holdout_gate,
        "combined": {**combined, "passed": not reasons, "reasons": tuple(reasons)},
    }


def deterministic_qc_sample(
    names: Iterable[str],
    *,
    seed: str,
    batch_key: str,
    count: int,
) -> tuple[str, ...]:
    frozen_seed = validate_seed(seed)
    unique = sorted({str(name) for name in names if str(name)})
    return tuple(
        sorted(
            unique,
            key=lambda name: _rank(frozen_seed, f"ongoing-qc:{batch_key}", name),
        )[: max(int(count), 0)]
    )


def ongoing_qc_plan(
    selected_count: int,
    sample_percent: float,
    remaining_capacity: int,
    *,
    capacity_gate_required: bool,
) -> dict[str, int | bool]:
    """Plan sampled QC without conflating monitoring with materialization.

    Capacity-gated mode fails closed when the complete requested sample cannot
    be assigned. Explicit non-blocking mode assigns the deterministic prefix
    that fits and records the remainder as risk-accepted unassigned monitoring.
    """
    selected = max(int(selected_count), 0)
    percent = float(sample_percent)
    if percent <= 0 or percent > 100:
        raise ValueError("Ongoing QC sample percent must be greater than 0 and at most 100")
    requested = min(
        selected,
        max(1 if selected else 0, int(math.ceil(selected * percent / 100.0))),
    )
    assigned = min(requested, max(int(remaining_capacity), 0))
    unassigned = requested - assigned
    return {
        "requested": requested,
        "assigned": assigned,
        "unassigned": unassigned,
        "capacity_gate_required": bool(capacity_gate_required),
        "blocked": bool(capacity_gate_required and unassigned),
    }
