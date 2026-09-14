"""Atomic replacement of obsolete, unhandled matching generations."""

from __future__ import annotations

import json
from typing import Any, Iterable

import frappe


CHUNK_SIZE = 1_000
FINAL_REVIEW_STATUSES = {"Agreed", "Adjudicated"}
HISTORICAL_COMPONENT_STATES = {"Applied", "Corrected", "Superseded"}
HISTORICAL_CANDIDATE_STATES = {"Applied", "Reversed", "Superseded"}


def _chunks(values: Iterable[str]):
    ordered = tuple(sorted({str(value) for value in values if str(value)}))
    for offset in range(0, len(ordered), CHUNK_SIZE):
        yield ordered[offset : offset + CHUNK_SIZE]


def _source_scope(snapshot_json: str) -> tuple[str, ...]:
    snapshot = json.loads(snapshot_json or "{}")
    return tuple(
        sorted(
            {
                str(profile.get("source") or "")
                for profile in snapshot.get("source_profiles") or []
                if str(profile.get("source") or "")
            }
        )
    )


def _same_scope_runs(doctype: str, current: Any) -> tuple[str, ...]:
    scope = _source_scope(current.policy_snapshot_json)
    rows = frappe.get_all(
        doctype,
        filters={
            "name": ["!=", current.name],
            "status": ["in", ["Ready", "Active", "Completed"]],
        },
        fields=["name", "policy_snapshot_json"],
        limit_page_length=10_000,
    )
    return tuple(
        sorted(
            str(row.name)
            for row in rows
            if _source_scope(row.policy_snapshot_json) == scope
        )
    )


def _update_names(doctype: str, names: Iterable[str], values: dict[str, Any]) -> int:
    total = 0
    assignments = ", ".join(f"`{field}`=%s" for field in values)
    for chunk in _chunks(names):
        placeholders = ", ".join(["%s"] * len(chunk))
        frappe.db.sql(
            f"UPDATE `tab{doctype}` SET {assignments} "
            f"WHERE name IN ({placeholders})",
            tuple(values.values()) + chunk,
        )
        total += len(chunk)
    return total


def supersede_prior_canary_generations(current: Any) -> dict[str, int]:
    """Retire only unhandled Tiered work after ``current`` fully succeeds."""
    prior_runs = _same_scope_runs("CCD Match Canary Run", current)
    if not prior_runs:
        return {
            "superseded_canary_runs": 0,
            "superseded_recommendations": 0,
            "superseded_component_reviews": 0,
        }
    recommendations: list[Any] = []
    for chunk in _chunks(prior_runs):
        recommendations.extend(
            frappe.get_all(
                "CCD Match Recommendation",
                filters={
                    "canary_run": ["in", chunk],
                    "status": ["in", ["Proposed", "Approved", "Exception"]],
                    "rollout_state": ["in", ["Available", "Held"]],
                },
                fields=["name", "component_review"],
                limit_page_length=100_000,
            )
        )
    recommendation_names = tuple(str(row.name) for row in recommendations)
    component_names = tuple(
        sorted(
            {
                str(row.component_review)
                for row in recommendations
                if str(row.component_review or "")
            }
        )
    )
    component_rows = []
    for chunk in _chunks(component_names):
        component_rows.extend(
            frappe.get_all(
                "CCD Match Component Review",
                filters={"name": ["in", chunk]},
                fields=["name", "review_status", "materialization_status"],
                limit_page_length=100_000,
            )
        )
    component_updates: dict[tuple[tuple[str, Any], ...], list[str]] = {}
    for row in component_rows:
        values: dict[str, Any] = {"stale": 1}
        if str(row.review_status or "") not in FINAL_REVIEW_STATUSES:
            values["review_status"] = "Stale"
        if str(row.materialization_status or "") not in HISTORICAL_COMPONENT_STATES:
            values["materialization_status"] = "Superseded"
        component_updates.setdefault(tuple(sorted(values.items())), []).append(
            str(row.name)
        )
    for value_items, names in component_updates.items():
        _update_names("CCD Match Component Review", names, dict(value_items))
    _update_names(
        "CCD Match Recommendation",
        recommendation_names,
        {"rollout_state": "Superseded"},
    )
    _update_names("CCD Match Canary Run", prior_runs, {"status": "Superseded"})
    return {
        "superseded_canary_runs": len(prior_runs),
        "superseded_recommendations": len(recommendation_names),
        "superseded_component_reviews": len(component_rows),
    }


def supersede_prior_queue_generations(current: Any) -> dict[str, int]:
    """Retire only unhandled Splink work after ``current`` fully succeeds."""
    prior_runs = _same_scope_runs("CCD Match Review Queue Run", current)
    if not prior_runs:
        return {
            "superseded_queue_runs": 0,
            "superseded_candidates": 0,
            "stale_review_batches": 0,
        }
    candidates: list[Any] = []
    for chunk in _chunks(prior_runs):
        candidates.extend(
            frappe.get_all(
                "CCD Match Review Candidate",
                filters={
                    "queue_run": ["in", chunk],
                    "materialization_status": [
                        "not in",
                        sorted(HISTORICAL_CANDIDATE_STATES),
                    ],
                },
                fields=[
                    "name", "review_status", "materialization_status",
                    "assigned_review_batch",
                ],
                limit_page_length=100_000,
            )
        )
    batch_names = tuple(
        sorted(
            {
                str(row.assigned_review_batch)
                for row in candidates
                if str(row.assigned_review_batch or "")
            }
        )
    )
    candidate_updates: dict[tuple[tuple[str, Any], ...], list[str]] = {}
    for row in candidates:
        values: dict[str, Any] = {
            "stale": 1,
            "materialization_status": "Superseded",
        }
        if str(row.review_status or "") not in FINAL_REVIEW_STATUSES:
            values["review_status"] = "Stale"
        candidate_updates.setdefault(tuple(sorted(values.items())), []).append(
            str(row.name)
        )
    for value_items, names in candidate_updates.items():
        _update_names("CCD Match Review Candidate", names, dict(value_items))
    _update_names("CCD Match Review Batch", batch_names, {"status": "Stale"})
    _update_names("CCD Match Review Queue Run", prior_runs, {"status": "Superseded"})
    return {
        "superseded_queue_runs": len(prior_runs),
        "superseded_candidates": len(candidates),
        "stale_review_batches": len(batch_names),
    }
