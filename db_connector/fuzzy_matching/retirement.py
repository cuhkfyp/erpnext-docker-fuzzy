"""Pure helpers for retiring identity state whose source records disappeared."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


FINAL_REVIEW_STATUSES = frozenset({"Agreed", "Adjudicated"})
HISTORICAL_MATERIALIZATION_STATUSES = frozenset(
    {"Applied", "Corrected", "Reversed", "Superseded"}
)
CURRENT_MEMBERSHIP_STATUSES = frozenset({"Active", "Needs Revalidation"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def stable_scope_fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def stale_review_status(current: str | None) -> str:
    """Keep completed human outcomes while closing unfinished review work."""
    value = str(current or "")
    return value if value in FINAL_REVIEW_STATUSES else "Stale"


def stale_materialization_status(current: str | None) -> str:
    """Keep immutable applied/corrected history; stale only unfinished work."""
    value = str(current or "")
    return value if value in HISTORICAL_MATERIALIZATION_STATUSES else "Stale"


def group_retirement_targets(
    memberships: Iterable[Mapping[str, Any]], existing_record_ids: Iterable[str]
) -> dict[str, Any]:
    """Plan component-safe Group/Membership lifecycle changes.

    A Group with fewer than two surviving current members ends, including any
    surviving singleton Membership.  A Group with at least two survivors and
    at least one retired member remains present but every survivor requires
    revalidation.
    """
    existing = {str(item) for item in existing_record_ids}
    by_group: dict[str, list[Mapping[str, Any]]] = {}
    for row in memberships:
        if str(row.get("status") or "") not in CURRENT_MEMBERSHIP_STATUSES:
            continue
        by_group.setdefault(str(row.get("identity_group") or ""), []).append(row)

    end_memberships: list[str] = []
    revalidate_memberships: list[str] = []
    end_groups: list[str] = []
    revalidate_groups: list[str] = []
    survivor_counts: dict[str, int] = {}
    for group_name, rows in sorted(by_group.items()):
        surviving = [row for row in rows if str(row.get("ccd_master") or "") in existing]
        retired = [row for row in rows if str(row.get("ccd_master") or "") not in existing]
        if not retired:
            continue
        survivor_counts[group_name] = len(surviving)
        if len(surviving) < 2:
            end_groups.append(group_name)
            end_memberships.extend(str(row.get("name") or "") for row in rows)
        else:
            revalidate_groups.append(group_name)
            end_memberships.extend(str(row.get("name") or "") for row in retired)
            revalidate_memberships.extend(
                str(row.get("name") or "") for row in surviving
            )

    return {
        "end_memberships": tuple(sorted(filter(None, end_memberships))),
        "revalidate_memberships": tuple(
            sorted(filter(None, revalidate_memberships))
        ),
        "end_groups": tuple(sorted(filter(None, end_groups))),
        "revalidate_groups": tuple(sorted(filter(None, revalidate_groups))),
        "survivor_counts": survivor_counts,
    }
