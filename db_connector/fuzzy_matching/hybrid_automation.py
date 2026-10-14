"""Pure deterministic helpers for bounded lower-score hybrid automation."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def bounded_hybrid_pairs(
    rows: Iterable[Mapping[str, Any]],
    *,
    component_limit: int,
    allowed_sources: Iterable[str],
) -> tuple[dict[str, Any], ...]:
    """Return a stable highest-score slice within the frozen source scope.

    The governed Development proposal currently admits isolated pairs only, so
    every returned row is one complete two-record component.
    """
    limit = max(int(component_limit), 1)
    sources = frozenset(str(value) for value in allowed_sources if str(value))
    eligible = [
        dict(row)
        for row in rows
        if (not sources or str(row.get("source_pair") or "") in sources)
    ]
    eligible.sort(
        key=lambda row: (
            -float(row.get("probabilistic_score") or 0),
            str(row.get("name") or ""),
        )
    )
    return tuple(eligible[:limit])
