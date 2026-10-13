"""Pure helpers for zero-write lower-score hybrid research.

This module deliberately does not authorize materialization.  It classifies
already-scored Splink Review pairs into explainable deterministic
corroboration strata so a prospective rule can later be frozen and validated.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable, Mapping

from . import normalization as norm
from .types import EvidenceLevel


CONFIRMED_BIRTHDAY_KEYS = frozenset(
    {"1", "true", "yes", "y", "confirmed", "exact"}
)
STRONG_NAME_LEVELS = frozenset(
    {
        EvidenceLevel.EXACT.value,
        EvidenceLevel.CLOSE.value,
        EvidenceLevel.PHONETIC.value,
    }
)
HYBRID_RESEARCH_RULE_VERSION = "hybrid-research-v1"
EXPLORATORY_RARE_ROUTE_LIMIT = 10
EXPLORATORY_ROUTE_MINIMUM = 20


def _level(evidence: Mapping[str, Any], attribute: str) -> str:
    item = evidence.get(attribute)
    value = getattr(item, "level", None)
    return str(getattr(value, "value", value) or "missing")


def birthday_key_confirmed(value: Any) -> bool:
    """Return whether a source explicitly marks its birthday as confirmed."""
    return norm.text(value).casefold() in CONFIRMED_BIRTHDAY_KEYS


def quality_aware_birthday(
    left_value: Any,
    right_value: Any,
    left_key: Any,
    right_key: Any,
) -> dict[str, Any]:
    """Classify asymmetric CCD birthday evidence.

    Matching normalized values are positive even when a source represents an
    uncertain date as ``YYYY-01-01``.  A mismatch is negative only when both
    sources explicitly confirm their birthdays; otherwise it is neutral.
    """
    left = norm.birthday(left_value)
    right = norm.birthday(right_value)
    left_confirmed = birthday_key_confirmed(left_key)
    right_confirmed = birthday_key_confirmed(right_key)
    if not left or not right:
        status = "birthday_missing"
        positive = conflict = False
    elif left == right:
        status = (
            "birthday_exact_confirmed"
            if left_confirmed and right_confirmed
            else "birthday_exact_unconfirmed"
        )
        positive = True
        conflict = False
    elif left_confirmed and right_confirmed:
        status = "birthday_mismatch_confirmed"
        positive = False
        conflict = True
    else:
        status = "birthday_mismatch_uncertain_neutral"
        positive = conflict = False
    return {
        "status": status,
        "positive": positive,
        "conflict": conflict,
        "left_confirmed": left_confirmed,
        "right_confirmed": right_confirmed,
    }


def classify_corroboration(
    evidence: Mapping[str, Any],
    *,
    left_birthday: Any,
    right_birthday: Any,
    left_birthday_key: Any,
    right_birthday_key: Any,
    trusted_identifiers: Iterable[str] = (),
) -> dict[str, Any]:
    """Return explainable research routes without making an identity decision."""
    trusted = frozenset(str(item) for item in trusted_identifiers)
    chinese_exact = all(
        _level(evidence, attribute) == EvidenceLevel.EXACT.value
        for attribute in ("chi_surname", "chi_firstname")
    )
    english_exact = all(
        _level(evidence, attribute) == EvidenceLevel.EXACT.value
        for attribute in ("eng_surname", "eng_firstname")
    )
    chinese_strong = all(
        _level(evidence, attribute) in STRONG_NAME_LEVELS
        for attribute in ("chi_surname", "chi_firstname")
    )
    english_strong = all(
        _level(evidence, attribute) in STRONG_NAME_LEVELS
        for attribute in ("eng_surname", "eng_firstname")
    )
    exact_name = chinese_exact or english_exact
    strong_name = chinese_strong or english_strong
    name_support = any(
        _level(evidence, attribute)
        in {
            EvidenceLevel.EXACT.value,
            EvidenceLevel.CLOSE.value,
            EvidenceLevel.PHONETIC.value,
            EvidenceLevel.WEAK.value,
        }
        for attribute in (
            "chi_surname",
            "chi_firstname",
            "eng_surname",
            "eng_firstname",
        )
    )

    birthday = quality_aware_birthday(
        left_birthday,
        right_birthday,
        left_birthday_key,
        right_birthday_key,
    )
    exact_secondary = []
    if birthday["positive"]:
        exact_secondary.append("birthday")
    for attribute in ("phone", "email"):
        if _level(evidence, attribute) == EvidenceLevel.EXACT.value:
            exact_secondary.append(attribute)

    trusted_exact = sorted(
        attribute
        for attribute in trusted
        if _level(evidence, attribute) == EvidenceLevel.EXACT.value
    )
    trusted_conflicts = sorted(
        attribute
        for attribute in trusted
        if _level(evidence, attribute) == EvidenceLevel.DISAGREE.value
    )
    unverified_exact = sorted(
        attribute
        for attribute in ("hkid", "hksr_num")
        if attribute not in trusted
        and _level(evidence, attribute) == EvidenceLevel.EXACT.value
    )

    routes = []
    if exact_name and birthday["positive"]:
        routes.append(f"exact_name+{birthday['status']}")
    if exact_name and "phone" in exact_secondary:
        routes.append("exact_name+phone_exact")
    if exact_name and "email" in exact_secondary:
        routes.append("exact_name+email_exact")
    if exact_name and unverified_exact:
        routes.append("exact_name+unverified_identifier_exact")
    if strong_name and len(exact_secondary) >= 2:
        routes.append("strong_name+two_secondary_exact")
    if strong_name and unverified_exact and exact_secondary:
        routes.append("strong_name+unverified_identifier+secondary_exact")
    elif name_support and unverified_exact and exact_secondary:
        routes.append("name_support+unverified_identifier+secondary_exact")
    if not strong_name and name_support and len(exact_secondary) >= 2:
        routes.append("name_support+two_secondary_exact")
    if strong_name and trusted_exact:
        routes.append("strong_name+trusted_identifier_exact")

    hard_conflicts = []
    if birthday["conflict"]:
        hard_conflicts.append("birthday_mismatch_confirmed")
    hard_conflicts.extend(
        f"trusted_identifier_conflict:{attribute}"
        for attribute in trusted_conflicts
    )
    return {
        "selected_for_research": bool(routes) and not hard_conflicts,
        "routes": tuple(sorted(set(routes))),
        "hard_conflicts": tuple(sorted(set(hard_conflicts))),
        "birthday_status": birthday["status"],
        "exact_name": exact_name,
        "strong_name": strong_name,
        "name_support": name_support,
        "exact_secondary": tuple(sorted(exact_secondary)),
        "trusted_identifier_exact": tuple(trusted_exact),
        "unverified_identifier_exact": tuple(unverified_exact),
    }


def lower_score_band(score: float, ultra_high_cutoff: float) -> str:
    value = float(score)
    cutoff = float(ultra_high_cutoff)
    if value >= cutoff:
        return "at_or_above_ultra_high"
    if value >= 0.999:
        return "0.999_to_ultra_high"
    if value >= 0.99:
        return "0.99_to_0.999"
    if value >= 0.95:
        return "0.95_to_0.99"
    return "below_0.95"


def _rank(seed: str, *values: Any) -> str:
    material = "\x1f".join([str(seed), *(str(value) for value in values)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _candidate_name(row: Mapping[str, Any]) -> str:
    return str(row.get("name") or row.get("candidate") or "")


def _row_routes(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({str(route) for route in row.get("routes", ()) if route}))


def _stratum_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("primary_route") or ""),
        str(row.get("score_band") or ""),
        str(row.get("source_pair") or ""),
        str(row.get("birthday_status") or ""),
    )


def _stratified_take(
    rows: Iterable[Mapping[str, Any]], count: int, seed: str
) -> list[dict[str, Any]]:
    """Take a deterministic round-robin sample across the visible strata."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for source in rows:
        row = dict(source)
        groups[_stratum_key(row)].append(row)
    for key, members in groups.items():
        members.sort(key=lambda item: _rank(seed, key, _candidate_name(item)))
    keys = sorted(groups, key=lambda key: _rank(seed, "stratum", *key))
    offsets = {key: 0 for key in keys}
    selected: list[dict[str, Any]] = []
    target = max(0, int(count))
    while len(selected) < target:
        progressed = False
        for key in keys:
            offset = offsets[key]
            if offset >= len(groups[key]):
                continue
            selected.append(groups[key][offset])
            offsets[key] += 1
            progressed = True
            if len(selected) >= target:
                break
        if not progressed:
            break
    return selected


def _route_allocations(
    capacities: Mapping[str, int], slots: int, seed: str
) -> dict[str, int]:
    """Give active routes a floor, then apportion remaining slots by size."""
    routes = sorted(capacities, key=lambda route: _rank(seed, "route", route))
    allocation = {route: 0 for route in routes}
    remaining = min(max(0, int(slots)), sum(max(0, value) for value in capacities.values()))

    # A balanced floor keeps a large route from hiding a smaller route.
    for _ in range(EXPLORATORY_ROUTE_MINIMUM):
        for route in routes:
            if not remaining:
                break
            if allocation[route] >= max(0, int(capacities[route])):
                continue
            allocation[route] += 1
            remaining -= 1
        if not remaining:
            break

    while remaining:
        residual = {
            route: max(0, int(capacities[route]) - allocation[route])
            for route in routes
        }
        total = sum(residual.values())
        if not total:
            break
        round_slots = min(remaining, total)
        raw = {
            route: round_slots * residual[route] / total for route in routes
        }
        added = {
            route: min(residual[route], int(raw[route])) for route in routes
        }
        for route, value in added.items():
            allocation[route] += value
            remaining -= value
        if not remaining:
            break
        remainder_order = sorted(
            (route for route in routes if allocation[route] < capacities[route]),
            key=lambda route: (
                -(raw[route] - int(raw[route])),
                _rank(seed, "remainder", route),
            ),
        )
        if not remainder_order:
            break
        for route in remainder_order:
            if not remaining:
                break
            if allocation[route] >= capacities[route]:
                continue
            allocation[route] += 1
            remaining -= 1
    return allocation


def deterministic_stratified_sample(
    rows: Iterable[Mapping[str, Any]],
    target_size: int,
    seed: str,
) -> dict[str, Any]:
    """Build a reproducible exploratory sample without changing candidates.

    Routes containing no more than ten candidates are included in full when
    the target permits. Remaining capacity gives every active primary route a
    balanced floor and then follows population size. Within a route, selection
    cycles across score band, source pair, and birthday state.
    """
    unique: dict[str, dict[str, Any]] = {}
    for source in rows:
        name = _candidate_name(source)
        routes = _row_routes(source)
        if not name or not routes:
            continue
        row = dict(source)
        row["name"] = name
        row["routes"] = routes
        unique[name] = row
    population = list(unique.values())
    target = min(max(0, int(target_size)), len(population))

    route_members: dict[str, set[str]] = defaultdict(set)
    for row in population:
        for route in row["routes"]:
            route_members[route].add(row["name"])
    route_counts = {
        route: len(names) for route, names in sorted(route_members.items())
    }
    rare_routes = tuple(
        route
        for route, count in route_counts.items()
        if count <= EXPLORATORY_RARE_ROUTE_LIMIT
    )

    for row in population:
        row["primary_route"] = min(
            row["routes"], key=lambda route: (route_counts[route], route)
        )

    mandatory = [
        row for row in population if set(row["routes"]).intersection(rare_routes)
    ]
    if len(mandatory) > target:
        selected = _stratified_take(mandatory, target, f"{seed}:rare")
    else:
        selected = sorted(
            mandatory,
            key=lambda row: (
                row["primary_route"],
                _rank(seed, "rare", row["name"]),
            ),
        )

    selected_names = {row["name"] for row in selected}
    remaining_rows = [row for row in population if row["name"] not in selected_names]
    route_pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in remaining_rows:
        route_pools[row["primary_route"]].append(row)
    allocations = _route_allocations(
        {route: len(pool) for route, pool in route_pools.items()},
        target - len(selected),
        seed,
    )
    for route in sorted(route_pools):
        selected.extend(
            _stratified_take(
                route_pools[route],
                allocations.get(route, 0),
                f"{seed}:{route}",
            )
        )

    selected_names = {row["name"] for row in selected}
    if len(selected) < target:
        selected.extend(
            _stratified_take(
                (row for row in population if row["name"] not in selected_names),
                target - len(selected),
                f"{seed}:top-up",
            )
        )

    selected.sort(
        key=lambda row: (
            row["primary_route"],
            row.get("score_band") or "",
            row.get("source_pair") or "",
            row.get("birthday_status") or "",
            _rank(seed, "final", row["name"]),
        )
    )
    for index, row in enumerate(selected, start=1):
        row["sample_order"] = index
    return {
        "rows": selected,
        "target_size": int(target_size),
        "selected_size": len(selected),
        "population_size": len(population),
        "population_route_counts": route_counts,
        "rare_routes_included_in_full": list(rare_routes),
    }
