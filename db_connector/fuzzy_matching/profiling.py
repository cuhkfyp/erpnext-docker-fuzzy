"""Run-scoped data-quality profiling for canonical attributes."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from . import normalization as norm
from .policy import MatchingPolicy


def _normalized(attribute: str, value: Any) -> str:
    if attribute in {"hkid", "hksr_num"}:
        return norm.identifier(value)
    if attribute == "phone":
        return norm.phone(value)
    if attribute == "email":
        return norm.email(value)
    if attribute == "birthday":
        return norm.birthday(value)
    if attribute.startswith("chi_"):
        return norm.chinese_compact(value)
    if attribute.startswith("eng_"):
        return norm.english_compact(value)
    return norm.text(value)


def profile_attributes(records: Iterable[dict[str, Any]], policy: MatchingPolicy) -> dict[str, Any]:
    rows = list(records)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[str(row.get("source") or row.get("ccd_reg_source") or "")].append(row)
    output: dict[str, Any] = {"record_count": len(rows), "sources": {}}
    source_values: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for source, source_rows in sorted(by_source.items()):
        attributes = {}
        for attribute in policy.attributes():
            values = [_normalized(attribute, policy.value(row, attribute)) for row in source_rows]
            present = [value for value in values if value]
            counts = Counter(present)
            source_values[source][attribute] = set(present)
            attributes[attribute] = {
                "coverage": len(present) / len(source_rows) if source_rows else 0.0,
                "present": len(present),
                "distinct": len(counts),
                "duplicate_values": sum(1 for count in counts.values() if count > 1),
                "duplicate_rows": sum(count for count in counts.values() if count > 1),
            }
            if attribute == "hkid":
                attributes[attribute]["valid_rate"] = (
                    sum(norm.valid_hkid(value) for value in present) / len(present) if present else 0.0
                )
        output["sources"][source] = {"record_count": len(source_rows), "attributes": attributes}

    overlaps = {}
    sources = sorted(source_values)
    for attribute in policy.attributes():
        attribute_overlap = {}
        for index, left_source in enumerate(sources):
            for right_source in sources[index + 1 :]:
                overlap = source_values[left_source][attribute] & source_values[right_source][attribute]
                attribute_overlap[f"{left_source}::{right_source}"] = len(overlap)
        overlaps[attribute] = attribute_overlap
    output["cross_source_distinct_overlap"] = overlaps
    return output
