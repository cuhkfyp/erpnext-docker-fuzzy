"""Multi-route cross-centre candidate generation."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
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

BLOCKING_VERSION = "pilot-blocking-1.4"


@dataclass(frozen=True)
class BlockingResult:
    pairs: tuple[CandidatePair, ...]
    skipped_blocks: tuple[str, ...]
    truncated: bool = False


def record_id(record: dict[str, Any]) -> str:
    return str(record.get("record_id") or record.get("name") or "")


def record_source(record: dict[str, Any]) -> str:
    return str(record.get("source") or record.get("ccd_reg_source") or "")


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
    retained_blocks: list[tuple[str, tuple[str, ...]]] = []
    for key, raw_ids in index.items():
        ids = tuple(sorted(set(raw_ids)))
        if len(ids) > policy.max_block_size:
            route = key.split(":", 1)[0]
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            skipped.append(f"{route}:{digest} ({len(ids)} records)")
            continue
        retained_blocks.append((key, ids))

    # Stronger and smaller blocks are processed first. This makes a bounded
    # candidate set deterministic and prevents broad name blocks from starving
    # exact identifiers, contacts, or date/name combinations.
    retained_blocks.sort(
        key=lambda item: (
            BLOCK_ROUTE_PRIORITY.get(item[0].split(":", 1)[0], 99),
            len(item[1]),
            hashlib.sha256(item[0].encode()).digest(),
        )
    )
    truncated = False
    for key, ids in retained_blocks:
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

    pairs = []
    for (left_id, right_id), routes in sorted(routes_by_pair.items()):
        source_pair = "::".join(sorted((record_source(by_id[left_id]), record_source(by_id[right_id]))))
        pairs.append(CandidatePair(left_id, right_id, source_pair, tuple(sorted(routes))))
    return BlockingResult(tuple(pairs), tuple(sorted(skipped)), truncated)
