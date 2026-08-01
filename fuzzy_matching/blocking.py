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

    for attribute in policy.trusted_global_identifiers:
        if not policy.globally_comparable(source, attribute):
            continue
        value = norm.identifier(policy.value(record, attribute))
        if value:
            keys.add(f"global_id:{attribute}:{value}")

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
    pinyin = norm.chinese_pinyin(chi_surname)
    if pinyin:
        keys.add(f"chi_surname_initial:{pinyin[0]}")

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
    truncated = False
    for key, ids in index.items():
        if len(ids) > policy.max_block_size:
            route = key.split(":", 1)[0]
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            skipped.append(f"{route}:{digest} ({len(ids)} records)")
            continue
        for left_id, right_id in combinations(sorted(set(ids)), 2):
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
    return BlockingResult(tuple(pairs), tuple(skipped), truncated)
