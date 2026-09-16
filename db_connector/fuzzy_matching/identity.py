"""Pure identity-resolution planning and fingerprint helpers.

The functions in this module deliberately have no Frappe dependency.  They
are used by the server-side materializer and by fast unit tests that exercise
the safety invariants without touching a live site.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import normalization as norm
from .canary import canonical_identity_groups, ordered_pair
from .policy import MatchingPolicy


FINGERPRINT_VERSION = "identity-evidence-v1"
MATERIALIZER_VERSION = "identity-materializer-v1"


def _normalized_attribute(attribute: str, value: Any) -> str:
    if attribute in {"chi_surname", "chi_firstname"}:
        return norm.chinese_compact(value)
    if attribute in {"eng_surname", "eng_firstname"}:
        return norm.english_words(value)
    if attribute == "phone":
        return norm.phone(value)
    if attribute == "email":
        return norm.email(value)
    if attribute == "birthday":
        return norm.birthday(value)
    if attribute in {"hkid", "hksr_num"}:
        return norm.identifier(value)
    return norm.text(value)


def identity_evidence_payload(
    record: Mapping[str, Any], policy: MatchingPolicy
) -> dict[str, Any]:
    """Return the governed, normalized evidence included in a fingerprint.

    Administrative fields and the document ``modified`` timestamp are not
    included.  A policy/source change is included because it changes how the
    raw fields are interpreted.
    """
    row = dict(record)
    source = str(row.get("source") or row.get("ccd_reg_source") or "")
    row["source"] = source
    attributes = {
        attribute: _normalized_attribute(attribute, policy.value(row, attribute))
        for attribute in sorted(policy.attributes())
    }
    return {
        "fingerprint_version": FINGERPRINT_VERSION,
        "policy_version": policy.version,
        "source": source,
        "attributes": attributes,
    }


def identity_fingerprint(record: Mapping[str, Any], policy: MatchingPolicy) -> str:
    payload = identity_evidence_payload(record, policy)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def expected_identity_fingerprints(
    values: Iterable[tuple[str, Any]],
) -> dict[str, str]:
    """Return only present frozen fingerprints and reject contradictory copies.

    In particular, a database NULL must remain absent; converting it with
    ``str(None)`` would create the bogus expected fingerprint ``"None"``.
    """
    output: dict[str, str] = {}
    for record_id, raw_fingerprint in values:
        fingerprint = str(raw_fingerprint or "").strip()
        if not fingerprint:
            continue
        key = str(record_id)
        prior = output.setdefault(key, fingerprint)
        if prior != fingerprint:
            raise ValueError("inconsistent_frozen_identity_fingerprints")
    return output


def snapshot_modified_conflicts(
    expected: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return records whose current modified value differs from the snapshot."""
    return tuple(
        sorted(
            str(record_id)
            for record_id, frozen_value in expected.items()
            if str(current.get(str(record_id)) or "")
            != str(frozen_value or "")
        )
    )


def complete_valid_hkid(record: Mapping[str, Any], policy: MatchingPolicy) -> str:
    """Return a globally governed complete HKID, otherwise an empty string."""
    source = str(record.get("source") or record.get("ccd_reg_source") or "")
    if not policy.globally_comparable(source, "hkid"):
        return ""
    raw = policy.value(dict(record), "hkid")
    if not norm.valid_hkid(raw):
        return ""
    return norm.identifier(raw)


def complete_hkid_conflicts(
    groups: Iterable[Iterable[str]],
    records: Mapping[str, Mapping[str, Any]],
    policy: MatchingPolicy,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return groups containing multiple distinct complete valid HKIDs."""
    output: list[tuple[str, tuple[str, ...]]] = []
    for group in groups:
        members = tuple(sorted({str(item) for item in group}))
        values = tuple(
            sorted(
                {
                    value
                    for record_id in members
                    if (value := complete_valid_hkid(records[record_id], policy))
                }
            )
        )
        if len(values) > 1:
            group_hash = hashlib.sha256("\x1f".join(members).encode()).hexdigest()
            output.append((group_hash, values))
    return tuple(output)


def fingerprint_scoped_exclusion_conflicts(
    groups: Iterable[Iterable[str]],
    fingerprints: Mapping[str, str],
    exclusions: Iterable[tuple[str, str, str, str]],
) -> tuple[tuple[str, str], ...]:
    """Return active Different pairs contradicted by a proposed Same group.

    Exclusions apply only while both governed identity fingerprints still
    match the values recorded by the human decision.  A later identity-data
    change therefore routes through fresh review instead of making an old
    exclusion permanent.
    """
    group_for = {
        str(record_id): index
        for index, group in enumerate(groups)
        for record_id in group
    }
    conflicts: set[tuple[str, str]] = set()
    for left, right, left_fingerprint, right_fingerprint in exclusions:
        left = str(left)
        right = str(right)
        if group_for.get(left) != group_for.get(right):
            continue
        if (
            fingerprints.get(left) == str(left_fingerprint)
            and fingerprints.get(right) == str(right_fingerprint)
        ):
            conflicts.add(ordered_pair(left, right))
    return tuple(sorted(conflicts))


def normalize_partition(
    record_ids: Iterable[str], groups: Iterable[Iterable[str]]
) -> tuple[tuple[str, ...], ...]:
    """Validate and canonicalize a complete, non-overlapping partition."""
    expected = tuple(sorted({str(item) for item in record_ids if str(item)}))
    if not expected:
        raise ValueError("At least one record is required")
    normalized = tuple(
        sorted(tuple(sorted({str(item) for item in group if str(item)})) for group in groups)
    )
    if any(not group for group in normalized):
        raise ValueError("Identity groups cannot be empty")
    flattened = [item for group in normalized for item in group]
    if len(flattened) != len(set(flattened)):
        raise ValueError("A record appears in more than one identity group")
    if tuple(sorted(flattened)) != expected:
        raise ValueError("The identity partition must include every participating record")
    return normalized


def partition_from_same_pairs(
    record_ids: Iterable[str], same_pairs: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], ...]:
    """Build the complete transitive partition selected by human Same pairs."""
    return canonical_identity_groups(record_ids, same_pairs)


def component_map(
    edges: Iterable[tuple[str, str, str]],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Group recommendation edges by their already-frozen component key."""
    grouped: dict[str, set[tuple[str, str]]] = {}
    for component_key, left, right in edges:
        grouped.setdefault(str(component_key), set()).add(ordered_pair(left, right))
    return {
        key: tuple(sorted(pairs))
        for key, pairs in sorted(grouped.items())
    }


def validate_component_atomic_selection(
    all_edges: Iterable[tuple[str, str, str]],
    selected_edges: Iterable[tuple[str, str, str]],
) -> tuple[str, ...]:
    """Return selected components or reject a selector that split one."""
    available = component_map(all_edges)
    selected = component_map(selected_edges)
    for component_key, pairs in selected.items():
        if component_key not in available:
            raise ValueError(f"Unknown component: {component_key}")
        if pairs != available[component_key]:
            raise ValueError(f"Selection splits component: {component_key}")
    return tuple(sorted(selected))


def materialization_key(
    *,
    origin: str,
    origin_document: str,
    policy_version: str,
    groups: Iterable[Iterable[str]],
    exclusions: Iterable[tuple[str, str]] = (),
) -> str:
    canonical_groups = tuple(
        sorted(tuple(sorted(str(item) for item in group)) for group in groups)
    )
    canonical_exclusions = tuple(sorted(ordered_pair(*pair) for pair in exclusions))
    payload = {
        "materializer_version": MATERIALIZER_VERSION,
        "origin": str(origin),
        "origin_document": str(origin_document),
        "policy_version": str(policy_version),
        "groups": canonical_groups,
        "exclusions": canonical_exclusions,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class MaterializationPlan:
    record_ids: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    exclusions: tuple[tuple[str, str], ...]
    idempotency_key: str


def build_materialization_plan(
    *,
    origin: str,
    origin_document: str,
    policy_version: str,
    record_ids: Sequence[str],
    groups: Iterable[Iterable[str]],
    exclusions: Iterable[tuple[str, str]] = (),
) -> MaterializationPlan:
    normalized_groups = normalize_partition(record_ids, groups)
    normalized_exclusions = tuple(sorted({ordered_pair(*pair) for pair in exclusions}))
    allowed = set(str(item) for item in record_ids)
    if any(left not in allowed or right not in allowed for left, right in normalized_exclusions):
        raise ValueError("An exclusion references a record outside the decision")
    return MaterializationPlan(
        record_ids=tuple(sorted(allowed)),
        groups=normalized_groups,
        exclusions=normalized_exclusions,
        idempotency_key=materialization_key(
            origin=origin,
            origin_document=origin_document,
            policy_version=policy_version,
            groups=normalized_groups,
            exclusions=normalized_exclusions,
        ),
    )
