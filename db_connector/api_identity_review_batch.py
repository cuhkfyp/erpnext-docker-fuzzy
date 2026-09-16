"""Optional bounded assignments from the Splink Review Pool."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import Any

import frappe

RUN_DOCTYPE = "CCD Match Review Queue Run"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
BATCH_DOCTYPE = "CCD Match Review Batch"
ITEM_DOCTYPE = "CCD Match Review Batch Item"
OPEN_STATUSES = {"Unreviewed", "Partially Reviewed", "Positive Confirmation Required", "Needs Adjudication"}
FINAL_STATUSES = {"Agreed", "Adjudicated"}


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _candidate_rows(queue_run: str) -> list[Any]:
    return frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={
            "queue_run": queue_run,
            "stale": 0,
            "assigned_review_batch": ["is", "not set"],
            "review_status": ["in", sorted(OPEN_STATUSES)],
        },
        fields=["name", "priority_rank", "source_pair", "review_status"],
        order_by="priority_rank, name",
        limit_page_length=100_000,
    )


def _source_balanced(rows: list[Any], size: int) -> list[Any]:
    buckets: dict[str, deque[Any]] = defaultdict(deque)
    for row in rows:
        buckets[str(row.source_pair or "Unknown")].append(row)
    selected: list[Any] = []
    keys = sorted(buckets)
    while len(selected) < size and keys:
        remaining = []
        for key in keys:
            if buckets[key] and len(selected) < size:
                selected.append(buckets[key].popleft())
            if buckets[key]:
                remaining.append(key)
        keys = remaining
    return selected


@frappe.whitelist()
def create_review_batch(
    queue_run: str,
    batch_size: int | str,
    selection_method: str = "Highest Priority",
    assignee: str = "",
    due_at: str = "",
    candidate_names_json: str | list[str] | None = None,
) -> dict[str, Any]:
    _require_manager()
    size = int(batch_size or 0)
    if size <= 0:
        frappe.throw(
            "A Review Batch must assign at least one candidate. For zero assigned work, create no batch."
        )
    run = frappe.get_doc(RUN_DOCTYPE, queue_run)
    if run.status != "Ready":
        frappe.throw("Only a Ready Splink Review Pool can create assignments")
    allowed = {"Highest Priority", "Source Balanced", "Risk Targeted", "Manual"}
    if selection_method not in allowed:
        frappe.throw("Unsupported Review Batch selection method")
    available = _candidate_rows(run.name)
    by_name = {str(row.name): row for row in available}
    if selection_method == "Manual":
        requested = (
            json.loads(candidate_names_json or "[]")
            if isinstance(candidate_names_json, str)
            else (candidate_names_json or [])
        )
        names = tuple(dict.fromkeys(str(item) for item in requested))
        if not names or len(names) != size:
            frappe.throw("Manual selection must contain exactly the requested batch size")
        if any(name not in by_name for name in names):
            frappe.throw("A manually selected candidate is stale, closed, or already assigned")
        selected = [by_name[name] for name in names]
    elif selection_method == "Source Balanced":
        selected = _source_balanced(available, size)
    else:
        # Risk Targeted currently uses the approved probability priority order;
        # the frozen method name keeps the operational intent auditable.
        selected = available[:size]
    if len(selected) < size:
        frappe.throw(f"Only {len(selected)} unassigned eligible candidates remain")
    payload = {
        "queue_run": run.name,
        "selection_method": selection_method,
        "candidates": [str(row.name) for row in selected],
        "assignee": str(assignee or ""),
        "due_at": str(due_at or ""),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = frappe.db.get_value(
        BATCH_DOCTYPE, {"selection_fingerprint": fingerprint}, "name"
    )
    if existing:
        return {"batch": existing, "status": frappe.db.get_value(BATCH_DOCTYPE, existing, "status")}
    now = frappe.utils.now_datetime()
    batch = frappe.get_doc(
        {
            "doctype": BATCH_DOCTYPE,
            "queue_run": run.name,
            "selection_method": selection_method,
            "selection_fingerprint": fingerprint,
            "status": "Open",
            "batch_size": size,
            "priority_from": min(int(row.priority_rank or 0) for row in selected),
            "priority_to": max(int(row.priority_rank or 0) for row in selected),
            "filters_json": json.dumps(
                {"only_unassigned": True, "only_reproducible": True},
                sort_keys=True,
            ),
            "default_assignee": assignee or None,
            "due_at": due_at or None,
            "assigned_count": size,
            "complete_count": 0,
            "same_count": 0,
            "different_count": 0,
            "needs_adjudication_count": 0,
        }
    )
    for row in selected:
        batch.append(
            "items",
            {
                "review_candidate": row.name,
                "priority_rank": row.priority_rank,
                "assigned_to": assignee or None,
                "assigned_at": now,
                "due_at": due_at or None,
                "status": "Assigned",
            },
        )
    batch.insert(ignore_permissions=True)
    for row in selected:
        frappe.db.set_value(
            CANDIDATE_DOCTYPE,
            row.name,
            {
                "assigned_review_batch": batch.name,
                "assigned_to": assignee or None,
                "assigned_at": now,
                "due_at": due_at or None,
            },
            update_modified=False,
        )
    frappe.db.commit()
    return {
        "batch": batch.name,
        "status": "Open",
        "assigned_count": size,
        "remaining_unassigned_count": len(available) - size,
    }


def refresh_review_batch_for_candidate(candidate_name: str) -> None:
    values = frappe.db.get_value(
        CANDIDATE_DOCTYPE,
        candidate_name,
        ["assigned_review_batch", "review_status", "final_label", "stale"],
        as_dict=True,
    )
    if not values or not values.assigned_review_batch:
        return
    item_status = (
        "Stale"
        if values.stale
        else "Completed"
        if values.review_status in FINAL_STATUSES
        else "Partially Reviewed"
        if values.review_status != "Unreviewed"
        else "Assigned"
    )
    frappe.db.set_value(
        ITEM_DOCTYPE,
        {"parent": values.assigned_review_batch, "review_candidate": candidate_name},
        "status",
        item_status,
        update_modified=False,
    )
    candidate_rows = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={"assigned_review_batch": values.assigned_review_batch},
        fields=["review_status", "final_label", "stale", "count(name) as count"],
        group_by="review_status, final_label, stale",
    )
    complete = same = different = adjudication = stale = 0
    for row in candidate_rows:
        count = int(row.count or 0)
        stale += count if row.stale else 0
        complete += count if row.review_status in FINAL_STATUSES else 0
        same += count if row.final_label == "Same" else 0
        different += count if row.final_label == "Different" else 0
        adjudication += count if row.review_status == "Needs Adjudication" else 0
    batch_size = int(
        frappe.db.get_value(BATCH_DOCTYPE, values.assigned_review_batch, "batch_size") or 0
    )
    status = "Stale" if stale else "Completed" if complete == batch_size else "Open"
    frappe.db.set_value(
        BATCH_DOCTYPE,
        values.assigned_review_batch,
        {
            "status": status,
            "complete_count": complete,
            "same_count": same,
            "different_count": different,
            "needs_adjudication_count": adjudication,
        },
        update_modified=False,
    )


@frappe.whitelist()
def get_review_pool_summary(queue_run: str) -> dict[str, int]:
    run = frappe.get_doc(RUN_DOCTYPE, queue_run)
    total = frappe.db.count(CANDIDATE_DOCTYPE, {"queue_run": run.name})
    assigned = frappe.db.count(
        CANDIDATE_DOCTYPE,
        {"queue_run": run.name, "assigned_review_batch": ["is", "set"]},
    )
    return {
        "available_review_pool": int(total),
        "assigned_work": int(assigned),
        "unassigned_optional": int(total - assigned),
    }
