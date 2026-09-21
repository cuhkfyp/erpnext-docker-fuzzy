"""Multi-route cross-centre candidate generation."""

from __future__ import annotations

import ctypes
import gc
import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from . import normalization as norm
from .policy import MatchingPolicy
from .types import CandidatePair


BLOCK_ROUTE_PRIORITY = {
    "global_id": 0,
    "phone": 1,
    "email": 1,
    "unverified_id": 2,
    "dob_surname": 3,
    "chi_full": 4,
    "chi_pinyin_full": 5,
    "chi_given_sorted": 5,
    "eng_name": 6,
    "chi_name_prefix": 7,
}

BLOCKING_VERSION = "pilot-blocking-1.7"
HIGH_BLOCKING_VERSION = "pilot-high-blocking-1.7"
BROAD_NAME_ROUTES = frozenset({"chi_name_prefix", "eng_name"})
SPARSE_NOMINATION_ROUTES = BROAD_NAME_ROUTES | {"dob_surname"}
ROUTE_BITS = {
    route: 1 << index
    for index, route in enumerate(
        sorted({*BLOCK_ROUTE_PRIORITY, "dob_chi_full", "dob_eng_full"})
    )
}


def _release_unused_memory() -> None:
    """Release completed large blocking indexes before the next phase."""
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass


def _progress(
    callback: Callable[[str, int], None] | None,
    stage: str,
    count: int,
) -> None:
    if callback is not None:
        callback(stage, count)


@dataclass(frozen=True)
class BlockingResult:
    pairs: tuple[CandidatePair, ...]
    skipped_blocks: tuple[str, ...]
    truncated: bool = False


def _add_route(
    routes_by_pair: dict[tuple[str, str], int],
    pair_key: tuple[str, str],
    route: str,
) -> bool:
    """Add one route to a compact pair map and report whether the pair is new."""
    current = routes_by_pair.get(pair_key)
    routes_by_pair[pair_key] = (current or 0) | ROUTE_BITS[route]
    return current is None


def _materialize_pairs(
    routes_by_pair: dict[tuple[str, str], int],
    by_id: dict[str, dict[str, Any]],
) -> tuple[CandidatePair, ...]:
    """Build immutable candidates while releasing the large mutable map.

    Source-pair and route tuples are shared because only a small number of
    combinations exists.  Popping each mutable entry as its immutable result
    is created prevents candidate materialization from briefly retaining two
    complete million-row representations.
    """
    ordered_keys = sorted(routes_by_pair)
    source_pair_cache: dict[tuple[str, str], str] = {}
    routes_cache: dict[int, tuple[str, ...]] = {}

    def candidates() -> Iterable[CandidatePair]:
        for left_id, right_id in ordered_keys:
            mask = routes_by_pair.pop((left_id, right_id))
            left_source = record_source(by_id[left_id])
            right_source = record_source(by_id[right_id])
            source_key = tuple(sorted((left_source, right_source)))
            source_pair = source_pair_cache.get(source_key)
            if source_pair is None:
                source_pair = "::".join(source_key)
                source_pair_cache[source_key] = source_pair
            blocking_routes = routes_cache.get(mask)
            if blocking_routes is None:
                blocking_routes = tuple(
                    route
                    for route in sorted(ROUTE_BITS)
                    if mask & ROUTE_BITS[route]
                )
                routes_cache[mask] = blocking_routes
            yield CandidatePair(
                left_id,
                right_id,
                source_pair,
                blocking_routes,
            )

    return tuple(candidates())


def record_id(record: dict[str, Any]) -> str:
    return str(record.get("record_id") or record.get("name") or "")


def record_source(record: dict[str, Any]) -> str:
    return str(record.get("source") or record.get("ccd_reg_source") or "")


def _ratio(left: str, right: str) -> float:
    try:
        from rapidfuzz import fuzz

        return fuzz.ratio(left, right) / 100.0
    except Exception:
        return SequenceMatcher(None, left, right).ratio()


def _token_ratio(left: str, right: str) -> float:
    try:
        from rapidfuzz import fuzz

        return fuzz.token_set_ratio(left, right) / 100.0
    except Exception:
        return _ratio(" ".join(sorted(left.split())), " ".join(sorted(right.split())))


def _broad_name_values(
    route: str,
    by_id: dict[str, dict[str, Any]],
    policy: MatchingPolicy,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    if route == "dob_surname":
        primary = {
            item: norm.chinese_compact(
                f"{policy.value(record, 'chi_surname') or ''}"
                f"{policy.value(record, 'chi_firstname') or ''}"
            )
            for item, record in by_id.items()
        }
        secondary = {
            item: norm.chinese_pinyin(value) for item, value in primary.items()
        }
        tertiary = {
            item: norm.english_words(
                f"{policy.value(record, 'eng_surname') or ''} "
                f"{policy.value(record, 'eng_firstname') or ''}"
            )
            for item, record in by_id.items()
        }
        return primary, secondary, tertiary
    if route == "chi_name_prefix":
        primary = {
            item: norm.chinese_compact(policy.value(record, "chi_firstname"))
            for item, record in by_id.items()
        }
        secondary = {item: norm.chinese_pinyin(value) for item, value in primary.items()}
        return primary, secondary, {}
    primary = {
        item: norm.english_words(policy.value(record, "eng_firstname"))
        for item, record in by_id.items()
    }
    return primary, {}, {}


def _ranked_broad_candidates(
    route: str,
    blocks: list[tuple[str, ...]],
    by_id: dict[str, dict[str, Any]],
    policy: MatchingPolicy,
    existing_pairs: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return deterministic nearest-name pairs, sparse endpoints first.

    Each record nominates its closest name within every other source represented
    in its block. Ranking the nominated pairs by the smaller endpoint choice set
    prevents large integrations from starving records that have only one or a
    few possible cross-source counterparts.
    """
    primary, secondary, tertiary = _broad_name_values(route, by_id, policy)
    selector_counts: Counter[tuple[str, str]] = Counter()
    best: dict[tuple[str, str], tuple[float, bytes, tuple[str, str]]] = {}
    for ids in blocks:
        for left_id, right_id in combinations(ids, 2):
            left_source = record_source(by_id[left_id])
            right_source = record_source(by_id[right_id])
            pair = (left_id, right_id)
            if (
                not left_source
                or not right_source
                or left_source == right_source
                or pair in existing_pairs
            ):
                continue
            selectors = ((left_id, right_source), (right_id, left_source))
            selector_counts.update(selectors)
            score = 0.0
            if primary[left_id] and primary[right_id]:
                score = _ratio(primary[left_id], primary[right_id])
            if secondary and secondary[left_id] and secondary[right_id]:
                score = max(score, _token_ratio(secondary[left_id], secondary[right_id]))
            if tertiary and tertiary[left_id] and tertiary[right_id]:
                score = max(score, _token_ratio(tertiary[left_id], tertiary[right_id]))
            digest = hashlib.sha256(f"{route}:{left_id}:{right_id}".encode()).digest()
            for selector in selectors:
                current = best.get(selector)
                if current is None or score > current[0] or (
                    score == current[0] and digest < current[1]
                ):
                    best[selector] = (score, digest, pair)

    ranked: dict[tuple[str, str], tuple[int, float, bytes]] = {}
    for selector, (score, digest, pair) in best.items():
        scarcity = selector_counts[selector]
        current = ranked.get(pair)
        metadata = (scarcity, -score, digest)
        if current is None or metadata < current:
            ranked[pair] = metadata
    # These dictionaries can contain hundreds of thousands of entries. Drop
    # the two no-longer-needed copies before sorting the final rank keys.
    best.clear()
    selector_counts.clear()
    _release_unused_memory()
    return sorted(ranked, key=ranked.__getitem__)


def blocking_keys(record: dict[str, Any], policy: MatchingPolicy) -> set[str]:
    keys: set[str] = set()
    source = record_source(record)

    for attribute in ("hkid", "hksr_num"):
        raw_value = policy.value(record, attribute)
        value = norm.identifier(raw_value)
        if not value:
            continue
        globally_usable = policy.globally_comparable(source, attribute) and (
            attribute != "hkid" or norm.valid_hkid(raw_value)
        )
        if globally_usable:
            keys.add(f"global_id:{attribute}:{value}")
        else:
            # Unknown/local identifiers and incomplete, masked, or invalid
            # HKIDs are useful for finding audit examples, but never become
            # deterministic global-identifier evidence.
            keys.add(f"unverified_id:{attribute}:{value}")

    phone = norm.phone(policy.value(record, "phone"))
    if phone:
        keys.add(f"phone:{phone}")
    email = norm.email(policy.value(record, "email"))
    if email:
        keys.add(f"email:{email}")

    chi_surname = norm.chinese_compact(policy.value(record, "chi_surname"))
    chi_firstname = norm.chinese_compact(policy.value(record, "chi_firstname"))
    chi_full = f"{chi_surname}{chi_firstname}"
    if chi_full:
        keys.add(f"chi_full:{chi_full}")
    if chi_surname and chi_firstname:
        # Recover bounded spelling variants without opening a surname-only
        # block. Exact full-name pinyin covers homophones, while the sorted
        # given-name key covers transpositions. Both retain the exact surname
        # and are materially narrower than character n-gram routes.
        chi_firstname_pinyin = norm.chinese_pinyin(chi_firstname).replace(" ", "")
        if chi_firstname_pinyin:
            keys.add(f"chi_pinyin_full:{chi_surname}:{chi_firstname_pinyin}")
        if len(chi_firstname) >= 2:
            keys.add(f"chi_given_sorted:{chi_surname}:{''.join(sorted(chi_firstname))}")
        # A surname initial alone creates enormous, low-value blocks and can
        # consume the global candidate cap before stronger evidence is seen.
        keys.add(f"chi_name_prefix:{chi_surname}:{chi_firstname[:1]}")

    eng_surname = norm.english_compact(policy.value(record, "eng_surname"))
    eng_firstname = norm.english_compact(policy.value(record, "eng_firstname"))
    if eng_surname and eng_firstname:
        keys.add(f"eng_name:{eng_surname}:{eng_firstname[:2]}")

    dob = norm.birthday(policy.value(record, "birthday"))
    surname_key = chi_surname or eng_surname
    if dob and surname_key:
        keys.add(f"dob_surname:{dob}:{surname_key}")
    return keys


def deterministic_high_blocking_keys(
    record: dict[str, Any],
    policy: MatchingPolicy,
) -> set[str]:
    """Return every exact route that can contribute to deterministic High.

    ``tiered_result`` can produce High only from an exact trusted global
    identifier, or from an exact Chinese/English full name plus an exact
    birthday, phone, or email. Phone and email blocks already discover the
    latter two cases. The two birthday/full-name intersections discover the
    remaining case without enumerating the much larger general Review
    universe. Candidates are still scored afterward, so conflicting trusted
    identifiers continue to gate a discovered pair out of High.
    """
    return _deterministic_high_blocking_keys_for_routes(
        record,
        policy,
        {
            "global_id",
            "phone",
            "email",
            "dob_chi_full",
            "dob_eng_full",
        },
    )


def _deterministic_high_blocking_keys_for_routes(
    record: dict[str, Any],
    policy: MatchingPolicy,
    routes: set[str],
) -> set[str]:
    """Return deterministic-High keys for only the requested route group."""
    keys: set[str] = set()
    source = record_source(record)

    if "global_id" in routes:
        for attribute in policy.trusted_global_identifiers:
            if not policy.globally_comparable(source, attribute):
                continue
            raw_value = policy.value(record, attribute)
            if attribute == "hkid" and not norm.valid_hkid(raw_value):
                continue
            value = norm.identifier(raw_value)
            if value:
                keys.add(f"global_id:{attribute}:{value}")

    if "phone" in routes:
        phone = norm.phone(policy.value(record, "phone"))
        if phone:
            keys.add(f"phone:{phone}")
    if "email" in routes:
        email = norm.email(policy.value(record, "email"))
        if email:
            keys.add(f"email:{email}")

    if not {"dob_chi_full", "dob_eng_full"} & routes:
        return keys
    birthday = norm.birthday(policy.value(record, "birthday"))
    if not birthday:
        return keys

    if "dob_chi_full" in routes:
        chi_surname = norm.chinese_compact(policy.value(record, "chi_surname"))
        chi_firstname = norm.chinese_compact(policy.value(record, "chi_firstname"))
        if chi_surname and chi_firstname:
            keys.add(f"dob_chi_full:{birthday}:{chi_surname}:{chi_firstname}")

    if "dob_eng_full" in routes:
        eng_surname = norm.english_compact(policy.value(record, "eng_surname"))
        eng_firstname = norm.english_compact(policy.value(record, "eng_firstname"))
        if eng_surname and eng_firstname:
            keys.add(f"dob_eng_full:{birthday}:{eng_surname}:{eng_firstname}")
    return keys


def generate_deterministic_high_candidate_pairs(
    records: Iterable[dict[str, Any]],
    policy: MatchingPolicy,
    *,
    progress: Callable[[str, int], None] | None = None,
) -> BlockingResult:
    """Generate the complete bounded candidate universe for deterministic High.

    The route set is logically sufficient for the current deterministic High
    rule (see ``deterministic_high_blocking_keys``). A result is safe to use
    only when neither ``truncated`` nor ``skipped_blocks`` is set.
    """
    rows = list(records)
    by_id = {record_id(row): row for row in rows if record_id(row)}
    rows.clear()
    routes_by_pair: dict[tuple[str, str], int] = {}
    skipped: list[str] = []
    truncated = False
    high_routes = {
        "global_id",
        "phone",
        "email",
        "dob_chi_full",
        "dob_eng_full",
    }
    priority_groups: dict[int, set[str]] = defaultdict(set)
    for route in high_routes:
        priority_groups[BLOCK_ROUTE_PRIORITY.get(route, 2)].add(route)

    # Build only one priority-equivalent route group at a time. Sorting all
    # blocks within that group retains the original priority/size/hash order,
    # while releasing the large unique-value index before the next group.
    for priority, group_routes in sorted(priority_groups.items()):
        index: dict[str, list[str]] = defaultdict(list)
        for row_id, row in by_id.items():
            for key in _deterministic_high_blocking_keys_for_routes(
                row,
                policy,
                group_routes,
            ):
                index[key].append(row_id)
        _progress(progress, f"high_index_priority_{priority}", len(index))

        blocks: list[tuple[str, tuple[str, ...]]] = []
        for key, raw_ids in index.items():
            ids = tuple(sorted(set(raw_ids)))
            route = key.split(":", 1)[0]
            if len(ids) > policy.max_block_size:
                digest = hashlib.sha256(key.encode()).hexdigest()[:12]
                skipped.append(f"{route}:{digest} ({len(ids)} records)")
                continue
            blocks.append((key, ids))
        index.clear()
        _release_unused_memory()

        blocks.sort(
            key=lambda item: (
                len(item[1]),
                hashlib.sha256(item[0].encode()).digest(),
            )
        )
        _progress(progress, f"high_blocks_priority_{priority}", len(blocks))
        for key, ids in blocks:
            route = key.split(":", 1)[0]
            for left_id, right_id in combinations(ids, 2):
                left_source = record_source(by_id[left_id])
                right_source = record_source(by_id[right_id])
                if not left_source or not right_source or left_source == right_source:
                    continue
                pair_key = (left_id, right_id)
                if (
                    pair_key not in routes_by_pair
                    and len(routes_by_pair) >= policy.max_candidate_pairs
                ):
                    truncated = True
                    break
                was_new = _add_route(routes_by_pair, pair_key, route)
                if was_new and len(routes_by_pair) % 100_000 == 0:
                    _progress(progress, "high_candidate_progress", len(routes_by_pair))
            if truncated:
                break
        blocks.clear()
        _release_unused_memory()
        if truncated:
            break
    _progress(progress, "high_candidates_ready", len(routes_by_pair))
    pairs = _materialize_pairs(routes_by_pair, by_id)
    _progress(progress, "high_candidates_materialized", len(pairs))
    return BlockingResult(pairs, tuple(sorted(skipped)), truncated)


def generate_candidate_pairs(
    records: Iterable[dict[str, Any]],
    policy: MatchingPolicy,
    *,
    progress: Callable[[str, int], None] | None = None,
) -> BlockingResult:
    rows = list(records)
    by_id = {record_id(row): row for row in rows if record_id(row)}
    rows.clear()
    index: dict[str, list[str]] = defaultdict(list)
    for row_id, row in by_id.items():
        for key in blocking_keys(row, policy):
            index[key].append(row_id)
    _progress(progress, "threshold_index_ready", len(index))

    routes_by_pair: dict[tuple[str, str], int] = {}
    skipped: list[str] = []
    strong_blocks: list[tuple[str, tuple[str, ...]]] = []
    nomination_blocks: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for key, raw_ids in index.items():
        ids = tuple(sorted(set(raw_ids)))
        if len(ids) > policy.max_block_size:
            route = key.split(":", 1)[0]
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            skipped.append(f"{route}:{digest} ({len(ids)} records)")
            continue
        route = key.split(":", 1)[0]
        if route in SPARSE_NOMINATION_ROUTES:
            nomination_blocks[route].append(ids)
        else:
            strong_blocks.append((key, ids))

    index.clear()
    _release_unused_memory()
    _progress(
        progress,
        "threshold_blocks_ready",
        len(strong_blocks) + sum(len(items) for items in nomination_blocks.values()),
    )

    # Retain every stronger exact candidate before the bounded name fallbacks.
    strong_blocks.sort(
        key=lambda item: (
            BLOCK_ROUTE_PRIORITY.get(item[0].split(":", 1)[0], 99),
            len(item[1]),
            hashlib.sha256(item[0].encode()).digest(),
        )
    )
    truncated = False
    for key, ids in strong_blocks:
        for left_id, right_id in combinations(ids, 2):
            left, right = by_id[left_id], by_id[right_id]
            left_source, right_source = record_source(left), record_source(right)
            if not left_source or not right_source or left_source == right_source:
                continue
            pair_key = (left_id, right_id)
            was_new = _add_route(routes_by_pair, pair_key, key.split(":", 1)[0])
            if was_new and len(routes_by_pair) % 100_000 == 0:
                _progress(progress, "threshold_candidate_progress", len(routes_by_pair))
            if len(routes_by_pair) >= policy.max_candidate_pairs:
                truncated = True
                break
        if truncated:
            break
    strong_blocks.clear()
    _release_unused_memory()

    # DOB+surname and broad prefix blocks can contain millions of
    # cross-products. DOB+surname first contributes one closest full-name
    # nomination per endpoint and other source. Selecting independently of the
    # stronger exact routes keeps this candidate definition stable and lets an
    # already-discovered pair retain DOB route provenance.
    if not truncated and nomination_blocks.get("dob_surname"):
        ranked_dob = _ranked_broad_candidates(
            "dob_surname",
            nomination_blocks["dob_surname"],
            by_id,
            policy,
            set(),
        )
        for pair_key in ranked_dob:
            was_new = _add_route(routes_by_pair, pair_key, "dob_surname")
            if was_new and len(routes_by_pair) >= policy.max_candidate_pairs:
                truncated = True
                break
        ranked_dob.clear()

    # The two broad name routes nominate independently against the same frozen
    # stronger-route universe, then round-robin their ranked nominations. This
    # preserves sparse-endpoint coverage without allowing either route to
    # starve the other if the shared policy budget is reached.
    broad_blocks = {
        route: blocks
        for route, blocks in nomination_blocks.items()
        if route in BROAD_NAME_ROUTES
    }
    if not truncated and broad_blocks:
        existing_pairs = set(routes_by_pair)
        ranked_by_route = {
            route: _ranked_broad_candidates(
                route,
                blocks,
                by_id,
                policy,
                existing_pairs,
            )
            for route, blocks in sorted(broad_blocks.items())
        }
        existing_pairs.clear()
        offsets = {route: 0 for route in ranked_by_route}
        active = list(sorted(ranked_by_route))
        while active and len(routes_by_pair) < policy.max_candidate_pairs:
            next_active = []
            for route in active:
                ranked = ranked_by_route[route]
                offset = offsets[route]
                added = False
                while offset < len(ranked):
                    pair_key = ranked[offset]
                    offset += 1
                    was_new = _add_route(routes_by_pair, pair_key, route)
                    if was_new:
                        added = True
                        break
                offsets[route] = offset
                if offset < len(ranked):
                    next_active.append(route)
                if len(routes_by_pair) >= policy.max_candidate_pairs:
                    break
                if not added and offset >= len(ranked):
                    continue
            active = next_active
        truncated = any(
            offsets[route] < len(ranked)
            for route, ranked in ranked_by_route.items()
        )
        ranked_by_route.clear()
        _release_unused_memory()

    nomination_blocks.clear()
    _release_unused_memory()
    _progress(progress, "threshold_candidates_ready", len(routes_by_pair))
    pairs = _materialize_pairs(routes_by_pair, by_id)
    _progress(progress, "threshold_candidates_materialized", len(pairs))
    return BlockingResult(pairs, tuple(sorted(skipped)), truncated)
