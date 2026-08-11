"""Multi-route cross-centre candidate generation."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable
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

BLOCKING_VERSION = "pilot-blocking-1.6"
BROAD_NAME_ROUTES = frozenset({"chi_name_prefix", "eng_name"})


@dataclass(frozen=True)
class BlockingResult:
    pairs: tuple[CandidatePair, ...]
    skipped_blocks: tuple[str, ...]
    truncated: bool = False


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
) -> tuple[dict[str, str], dict[str, str]]:
    if route == "chi_name_prefix":
        primary = {
            item: norm.chinese_compact(policy.value(record, "chi_firstname"))
            for item, record in by_id.items()
        }
        secondary = {item: norm.chinese_pinyin(value) for item, value in primary.items()}
        return primary, secondary
    primary = {
        item: norm.english_words(policy.value(record, "eng_firstname"))
        for item, record in by_id.items()
    }
    return primary, {}


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
    primary, secondary = _broad_name_values(route, by_id, policy)
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
            score = _ratio(primary[left_id], primary[right_id])
            if secondary:
                score = max(score, _token_ratio(secondary[left_id], secondary[right_id]))
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


def generate_candidate_pairs(
    records: Iterable[dict[str, Any]],
    policy: MatchingPolicy,
) -> BlockingResult:
    rows = list(records)
    by_id = {record_id(row): row for row in rows if record_id(row)}
    index: dict[str, list[str]] = defaultdict(list)
    for row_id, row in by_id.items():
        for key in blocking_keys(row, policy):
            index[key].append(row_id)

    routes_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)
    skipped: list[str] = []
    strong_blocks: list[tuple[str, tuple[str, ...]]] = []
    broad_blocks: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for key, raw_ids in index.items():
        ids = tuple(sorted(set(raw_ids)))
        if len(ids) > policy.max_block_size:
            route = key.split(":", 1)[0]
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            skipped.append(f"{route}:{digest} ({len(ids)} records)")
            continue
        route = key.split(":", 1)[0]
        if route in BROAD_NAME_ROUTES:
            broad_blocks[route].append(ids)
        else:
            strong_blocks.append((key, ids))

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
            routes_by_pair[pair_key].add(key.split(":", 1)[0])
            if len(routes_by_pair) >= policy.max_candidate_pairs:
                truncated = True
                break
        if truncated:
            break

    # Broad prefix blocks can contain millions of cross-products. Instead of
    # exhausting them in block order, each endpoint nominates its closest name
    # per other source. Routes then round-robin those nominations, prioritizing
    # sparse endpoints, until the shared policy budget is full.
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
                    was_present = pair_key in routes_by_pair
                    routes_by_pair[pair_key].add(route)
                    if not was_present:
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

    pairs = []
    for (left_id, right_id), routes in sorted(routes_by_pair.items()):
        source_pair = "::".join(sorted((record_source(by_id[left_id]), record_source(by_id[right_id]))))
        pairs.append(CandidatePair(left_id, right_id, source_pair, tuple(sorted(routes))))
    return BlockingResult(tuple(pairs), tuple(sorted(skipped)), truncated)
