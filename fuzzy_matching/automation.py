"""Pure helpers for continuous QC and bounded Tiered automation."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .metrics import wilson_interval


def current_shared_group(
    memberships: Iterable[Mapping[str, Any]], left_record: str, right_record: str
) -> str | None:
    """Return the one current group shared by both records, if one exists."""
    by_record: dict[str, set[str]] = {str(left_record): set(), str(right_record): set()}
    for row in memberships:
        record = str(row.get("ccd_master") or "")
        group = str(row.get("identity_group") or "")
        if record in by_record and group:
            by_record[record].add(group)
    shared = by_record[str(left_record)] & by_record[str(right_record)]
    if len(shared) > 1:
        raise ValueError("The QC pair shares more than one current Identity Group")
    return next(iter(shared), None)


def deterministic_qc_selection(
    run_name: str,
    candidates: Iterable[Mapping[str, Any]],
    limit: int,
) -> tuple[str, ...]:
    """Choose reproducible QC rows without depending on database row order."""
    bounded = max(int(limit), 0)
    ranked = sorted(
        candidates,
        key=lambda row: (
            hashlib.sha256(
                f"{run_name}\x1fcontinuous-qc\x1f{row.get('recommendation_key') or row.get('name')}".encode()
            ).hexdigest(),
            str(row.get("name") or ""),
        ),
    )
    return tuple(str(row["name"]) for row in ranked[:bounded])


def rolling_qc_summary(
    finalized_labels: Sequence[str], window_size: int
) -> dict[str, Any]:
    """Summarize only the latest bounded comparable Same/Different labels."""
    size = max(int(window_size), 1)
    comparable = [label for label in finalized_labels if label in {"Same", "Different"}][
        -size:
    ]
    same = sum(label == "Same" for label in comparable)
    different = sum(label == "Different" for label in comparable)
    total = same + different
    lower, upper = wilson_interval(same, total)
    return {
        "window_finalized": total,
        "same": same,
        "different": different,
        "precision": same / total if total else 0.0,
        "wilson_95": (lower, upper),
        "window_complete": total >= size,
    }


def cadence_due(next_assignment_at: datetime | None, now: datetime) -> bool:
    """A missing next date means the first governed cadence is due now."""
    return next_assignment_at is None or next_assignment_at <= now
