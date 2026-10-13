"""Persistent, non-authorizing review of lower-score hybrid research samples."""

from __future__ import annotations

import hashlib
import json
import traceback
from typing import Any

import frappe

from db_connector.api_fuzzy_evaluation import REVIEW_ROLE, SENSITIVE_ROLE
from db_connector.api_hybrid_research import (
    _load_records,
    preview_lower_score_hybrid_research,
)
from db_connector.fuzzy_matching.hybrid_research import HYBRID_RESEARCH_RULE_VERSION
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.models import build_evidence
from db_connector.fuzzy_matching.policy import MatchingPolicy
from db_connector.fuzzy_matching.security import mask_identifier


SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
QUEUE_DOCTYPE = "CCD Match Review Queue Run"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
BATCH_DOCTYPE = "CCD Hybrid Exploratory Batch"
ITEM_DOCTYPE = "CCD Hybrid Exploratory Item"
FIXED_SAMPLE_SIZE = 100
ACTIVE_BATCH_STATUSES = ("Building", "Reviewing", "Completed", "Completed with Stale Items")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_reviewer() -> None:
    roles = set(frappe.get_roles())
    if not {"System Manager", REVIEW_ROLE, SENSITIVE_ROLE}.intersection(roles):
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _has_sensitive_access() -> bool:
    roles = set(frappe.get_roles())
    return "System Manager" in roles or SENSITIVE_ROLE in roles


def _request_context(minimum_score: float | str) -> tuple[Any, float, float]:
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    queue_name = str(settings.authorized_splink_queue or "")
    if not queue_name or not frappe.db.exists(QUEUE_DOCTYPE, queue_name):
        frappe.throw("An existing authorized Splink Review Queue is required")
    queue = frappe.get_doc(QUEUE_DOCTYPE, queue_name)
    if queue.status != "Ready":
        frappe.throw("Hybrid exploratory review requires a Ready authorized queue")
    cutoff = float(settings.authorized_splink_cutoff or 0)
    floor = float(minimum_score or 0)
    review_threshold = float(queue.review_threshold or 0)
    if floor < review_threshold or floor >= cutoff:
        frappe.throw(
            f"Research minimum score must be at least {review_threshold:.12f} "
            f"and below {cutoff:.12f}"
        )
    return queue, cutoff, floor


def _selection_fingerprint(queue: Any, cutoff: float, floor: float) -> str:
    payload = {
        "purpose": "persistent-hybrid-exploratory-review",
        "queue_run": str(queue.name),
        "policy_version": str(queue.policy_version or ""),
        "policy_snapshot_sha256": hashlib.sha256(
            str(queue.policy_snapshot_json or "").encode("utf-8")
        ).hexdigest(),
        "rule_version": HYBRID_RESEARCH_RULE_VERSION,
        "minimum_score": f"{float(floor):.12f}",
        "frozen_ultra_high_cutoff": f"{float(cutoff):.12f}",
        "sample_size": FIXED_SAMPLE_SIZE,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _enqueue_builder(batch_name: str) -> None:
    frappe.enqueue(
        "db_connector.api_hybrid_exploratory_review.build_hybrid_exploratory_batch",
        queue="long",
        timeout=3_600,
        enqueue_after_commit=True,
        batch_name=batch_name,
    )


@frappe.whitelist()
def start_hybrid_exploratory_batch(
    minimum_score: float | str = 0.95,
) -> dict[str, Any]:
    """Create one idempotent request and build its exact sample off-request."""
    _require_manager()
    queue, cutoff, floor = _request_context(minimum_score)
    fingerprint = _selection_fingerprint(queue, cutoff, floor)
    existing = frappe.db.get_value(
        BATCH_DOCTYPE,
        {"selection_fingerprint": fingerprint},
        ["name", "status"],
        as_dict=True,
    )
    if existing:
        return {
            "batch": str(existing.name),
            "status": str(existing.status),
            "existing": True,
        }
    batch = frappe.get_doc(
        {
            "doctype": BATCH_DOCTYPE,
            "queue_run": queue.name,
            "status": "Building",
            "requested_by": frappe.session.user,
            "requested_at": frappe.utils.now_datetime(),
            "matching_policy": queue.matching_policy,
            "policy_version": queue.policy_version,
            "rule_version": HYBRID_RESEARCH_RULE_VERSION,
            "research_minimum_score": floor,
            "frozen_ultra_high_cutoff": cutoff,
            "selection_fingerprint": fingerprint,
            "target_count": FIXED_SAMPLE_SIZE,
            "sample_count": 0,
            "reviewed_count": 0,
            "remaining_count": 0,
            "same_count": 0,
            "different_count": 0,
            "unsure_count": 0,
            "stale_count": 0,
        }
    )
    batch.insert(ignore_permissions=True)
    frappe.db.commit()
    _enqueue_builder(batch.name)
    return {"batch": batch.name, "status": "Building", "existing": False}


def build_hybrid_exploratory_batch(batch_name: str) -> None:
    """Freeze the selected candidates atomically in a background worker."""
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.status != "Building":
        return
    try:
        preview = preview_lower_score_hybrid_research(
            minimum_score=float(batch.research_minimum_score),
            queue_run=str(batch.queue_run),
            exploratory_sample_size=FIXED_SAMPLE_SIZE,
        )
        sample = preview.get("exploratory_sample") or {}
        rows = list(sample.get("rows") or [])
        if not rows:
            frappe.throw("The exploratory selection returned no eligible pair")
        candidate_names = [str(row["candidate"]) for row in rows]
        candidates = {
            str(row.name): row
            for row in frappe.get_all(
                CANDIDATE_DOCTYPE,
                filters={"name": ["in", candidate_names]},
                fields=[
                    "name",
                    "queue_run",
                    "left_record",
                    "right_record",
                    "left_modified_at",
                    "right_modified_at",
                    "left_identity_fingerprint",
                    "right_identity_fingerprint",
                ],
                limit_page_length=len(candidate_names),
            )
        }
        if set(candidates) != set(candidate_names):
            frappe.throw("One or more selected Review Candidates no longer exist")
        if frappe.db.count(ITEM_DOCTYPE, {"exploratory_batch": batch.name}):
            frappe.throw("The exploratory batch already has a partial item manifest")

        for row in rows:
            candidate = candidates[str(row["candidate"])]
            if str(candidate.queue_run) != str(batch.queue_run):
                frappe.throw("A selected candidate belongs to another Review Queue")
            item_fingerprint = hashlib.sha256(
                f"{batch.name}\x1f{candidate.name}".encode("utf-8")
            ).hexdigest()
            frappe.get_doc(
                {
                    "doctype": ITEM_DOCTYPE,
                    "exploratory_batch": batch.name,
                    "sample_order": int(row["sample_order"]),
                    "review_candidate": candidate.name,
                    "status": "Unreviewed",
                    "primary_route": str(row["primary_route"]),
                    "all_routes": "\n".join(str(route) for route in row["routes"]),
                    "probabilistic_score": float(row["score"]),
                    "score_band": str(row["score_band"]),
                    "source_pair": str(row["source_pair"]),
                    "birthday_status": str(row["birthday_status"]),
                    "left_record": candidate.left_record,
                    "right_record": candidate.right_record,
                    "left_modified_at": candidate.left_modified_at,
                    "right_modified_at": candidate.right_modified_at,
                    "left_identity_fingerprint": candidate.left_identity_fingerprint,
                    "right_identity_fingerprint": candidate.right_identity_fingerprint,
                    "item_fingerprint": item_fingerprint,
                }
            ).insert(ignore_permissions=True)

        batch = frappe.get_doc(BATCH_DOCTYPE, batch.name)
        batch.status = "Reviewing"
        batch.derived_seed = str(sample.get("derived_seed") or "")
        batch.eligible_manifest_digest = str(
            sample.get("eligible_manifest_digest") or ""
        )
        batch.eligible_population_count = int(sample.get("population_size") or 0)
        batch.sample_count = len(rows)
        batch.remaining_count = len(rows)
        batch.route_counts_json = _json(sample.get("route_counts") or {})
        batch.score_band_counts_json = _json(sample.get("score_band_counts") or {})
        batch.source_pair_counts_json = _json(sample.get("source_pair_counts") or {})
        batch.birthday_evidence_counts_json = _json(
            sample.get("birthday_evidence_counts") or {}
        )
        batch.error_summary = ""
        batch.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            BATCH_DOCTYPE,
            batch_name,
            {
                "status": "Failed",
                "error_summary": f"hybrid_exploratory_build_failed:{type(exc).__name__}",
            },
            update_modified=False,
        )
        frappe.log_error(traceback.format_exc(), "CCD Hybrid Exploratory Build Failure")
        frappe.db.commit()
        raise


@frappe.whitelist()
def retry_hybrid_exploratory_batch(batch_name: str) -> dict[str, str]:
    _require_manager()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.status != "Failed":
        frappe.throw("Only a failed exploratory batch can be retried")
    if frappe.db.count(ITEM_DOCTYPE, {"exploratory_batch": batch.name}):
        frappe.throw("A failed batch with stored items requires investigation")
    batch.db_set(
        {"status": "Building", "error_summary": ""}, update_modified=False
    )
    frappe.db.commit()
    _enqueue_builder(batch.name)
    return {"batch": batch.name, "status": "Building"}


def _load_item_context(item_name: str) -> tuple[Any, Any, Any, MatchingPolicy]:
    item = frappe.get_doc(ITEM_DOCTYPE, item_name)
    batch = frappe.get_doc(BATCH_DOCTYPE, item.exploratory_batch)
    queue = frappe.get_doc(QUEUE_DOCTYPE, batch.queue_run)
    policy = MatchingPolicy.from_dict(json.loads(queue.policy_snapshot_json or "{}"))
    return item, batch, queue, policy


def _item_records_and_staleness(
    item: Any, policy: MatchingPolicy
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    records = _load_records({str(item.left_record), str(item.right_record)}, policy)
    left = records.get(str(item.left_record))
    right = records.get(str(item.right_record))
    reasons: list[str] = []
    if not left or not right:
        reasons.append("record_missing")
        return left, right, reasons
    if (
        str(left.get("modified") or "") != str(item.left_modified_at or "")
        or str(right.get("modified") or "") != str(item.right_modified_at or "")
    ):
        reasons.append("source_modified_after_sample")
    if (
        identity_fingerprint(left, policy)
        != str(item.left_identity_fingerprint or "")
        or identity_fingerprint(right, policy)
        != str(item.right_identity_fingerprint or "")
    ):
        reasons.append("identity_fingerprint_changed")
    candidate = frappe.db.get_value(
        CANDIDATE_DOCTYPE,
        item.review_candidate,
        [
            "stale",
            "review_status",
            "final_label",
            "automation_reserved",
            "assigned_review_batch",
            "automation_batch",
            "materialization_status",
        ],
        as_dict=True,
    )
    if not candidate:
        reasons.append("review_candidate_missing")
    else:
        if candidate.stale:
            reasons.append("candidate_marked_stale")
        if str(candidate.review_status or "") != "Unreviewed" or candidate.final_label:
            reasons.append("candidate_received_non_exploratory_review")
        if candidate.automation_reserved or candidate.automation_batch:
            reasons.append("candidate_reserved_or_batched")
        if candidate.assigned_review_batch:
            reasons.append("candidate_assigned_optional_review")
        if str(candidate.materialization_status or "Not Final") != "Not Final":
            reasons.append("candidate_materialization_started")
    return left, right, sorted(set(reasons))


@frappe.whitelist()
def get_hybrid_exploratory_item_evidence(item_name: str) -> dict[str, Any]:
    _require_reviewer()
    item, batch, _queue, policy = _load_item_context(item_name)
    left, right, stale_reasons = _item_records_and_staleness(item, policy)
    if not left or not right:
        return {
            "item": item.name,
            "stale": True,
            "stale_reasons": stale_reasons,
            "attributes": [],
            "left": {"source": "Left"},
            "right": {"source": "Right"},
            "research_only": True,
        }
    evidence = build_evidence(left, right, policy)
    sensitive = _has_sensitive_access()
    attributes = []
    for attribute in policy.attributes():
        comparison = evidence.get(attribute)
        left_value = str(policy.value(left, attribute) or "").strip()
        right_value = str(policy.value(right, attribute) or "").strip()
        if not sensitive:
            left_value = mask_identifier(left_value, visible_suffix=2)
            right_value = mask_identifier(right_value, visible_suffix=2)
        attributes.append(
            {
                "attribute": attribute,
                "left": left_value,
                "right": right_value,
                "comparison": str(
                    comparison.level.value if comparison else "not_compared"
                ),
            }
        )
    return {
        "item": item.name,
        "batch": batch.name,
        "candidate": item.review_candidate,
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "left": {"source": str(left.get("source") or "Left")},
        "right": {"source": str(right.get("source") or "Right")},
        "attributes": attributes,
        "sensitive_values_visible": sensitive,
        "research_only": True,
    }


def _refresh_batch_progress(batch_name: str, completed_by: str = "") -> dict[str, int]:
    rows = frappe.get_all(
        ITEM_DOCTYPE,
        filters={"exploratory_batch": batch_name},
        fields=["status", "exploratory_label", "count(name) as count"],
        group_by="status, exploratory_label",
    )
    reviewed = stale = same = different = unsure = 0
    for row in rows:
        count = int(row.count or 0)
        if row.status == "Reviewed":
            reviewed += count
        elif row.status == "Stale":
            stale += count
        if row.exploratory_label == "Same":
            same += count
        elif row.exploratory_label == "Different":
            different += count
        elif row.exploratory_label == "Unsure":
            unsure += count
    sample_count = int(
        frappe.db.get_value(BATCH_DOCTYPE, batch_name, "sample_count") or 0
    )
    remaining = max(sample_count - reviewed - stale, 0)
    values: dict[str, Any] = {
        "reviewed_count": reviewed,
        "remaining_count": remaining,
        "same_count": same,
        "different_count": different,
        "unsure_count": unsure,
        "stale_count": stale,
    }
    if sample_count and remaining == 0:
        values.update(
            {
                "status": "Completed with Stale Items" if stale else "Completed",
                "completed_by": completed_by or frappe.session.user,
                "completed_at": frappe.utils.now_datetime(),
            }
        )
    frappe.db.set_value(BATCH_DOCTYPE, batch_name, values, update_modified=False)
    return {
        "reviewed": reviewed,
        "remaining": remaining,
        "same": same,
        "different": different,
        "unsure": unsure,
        "stale": stale,
    }


def _next_unreviewed_item(batch_name: str, after_order: int = 0) -> str:
    filters: list[list[Any]] = [
        ["exploratory_batch", "=", batch_name],
        ["status", "=", "Unreviewed"],
    ]
    if after_order:
        filters.append(["sample_order", ">", int(after_order)])
    names = frappe.get_all(
        ITEM_DOCTYPE,
        filters,
        pluck="name",
        order_by="sample_order asc, name asc",
        limit_page_length=1,
    )
    if not names and after_order:
        names = frappe.get_all(
            ITEM_DOCTYPE,
            filters={"exploratory_batch": batch_name, "status": "Unreviewed"},
            pluck="name",
            order_by="sample_order asc, name asc",
            limit_page_length=1,
        )
    return str(names[0]) if names else ""


@frappe.whitelist()
def get_next_hybrid_exploratory_item(batch_name: str) -> dict[str, str]:
    _require_reviewer()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.status == "Building":
        frappe.throw("The exploratory sample is still being built")
    return {"batch": batch.name, "item": _next_unreviewed_item(batch.name)}


@frappe.whitelist()
def get_hybrid_exploratory_batch_status(batch_name: str) -> dict[str, Any]:
    """Return a small polling view without exposing frozen record identities."""
    _require_reviewer()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    return {
        "batch": batch.name,
        "status": str(batch.status or ""),
        "target_count": int(batch.target_count or 0),
        "sample_count": int(batch.sample_count or 0),
        "reviewed_count": int(batch.reviewed_count or 0),
        "remaining_count": int(batch.remaining_count or 0),
        "same_count": int(batch.same_count or 0),
        "different_count": int(batch.different_count or 0),
        "unsure_count": int(batch.unsure_count or 0),
        "stale_count": int(batch.stale_count or 0),
        "eligible_population_count": int(batch.eligible_population_count or 0),
        "derived_seed": str(batch.derived_seed or ""),
        "eligible_manifest_digest": str(batch.eligible_manifest_digest or ""),
        "error_summary": str(batch.error_summary or ""),
    }


@frappe.whitelist()
def submit_hybrid_exploratory_label(
    item_name: str,
    label: str,
    notes: str = "",
) -> dict[str, Any]:
    _require_reviewer()
    if label not in {"Same", "Different", "Unsure"}:
        frappe.throw("Exploratory label must be Same, Different, or Unsure")
    frappe.db.sql(
        "SELECT name FROM `tabCCD Hybrid Exploratory Item` WHERE name = %s FOR UPDATE",
        item_name,
    )
    item, batch, _queue, policy = _load_item_context(item_name)
    if batch.status != "Reviewing":
        frappe.throw("Only a Reviewing exploratory batch accepts labels")
    if item.status != "Unreviewed" or item.exploratory_label:
        frappe.throw("This exploratory item already has an immutable outcome")
    _left, _right, stale_reasons = _item_records_and_staleness(item, policy)
    if stale_reasons:
        frappe.db.set_value(
            ITEM_DOCTYPE,
            item.name,
            {"status": "Stale", "review_notes": "; ".join(stale_reasons)},
            update_modified=False,
        )
        progress = _refresh_batch_progress(batch.name, frappe.session.user)
        frappe.db.commit()
        return {
            "item": item.name,
            "status": "Stale",
            "stale_reasons": stale_reasons,
            "next_item": _next_unreviewed_item(batch.name, int(item.sample_order or 0)),
            "progress": progress,
        }
    frappe.db.set_value(
        ITEM_DOCTYPE,
        item.name,
        {
            "status": "Reviewed",
            "exploratory_label": label,
            "review_notes": str(notes or "").strip(),
            "reviewed_by": frappe.session.user,
            "reviewed_at": frappe.utils.now_datetime(),
        },
        update_modified=False,
    )
    progress = _refresh_batch_progress(batch.name, frappe.session.user)
    next_item = _next_unreviewed_item(batch.name, int(item.sample_order or 0))
    frappe.db.commit()
    return {
        "item": item.name,
        "status": "Reviewed",
        "label": label,
        "next_item": next_item,
        "progress": progress,
        "candidate_state_changed": False,
        "identity_state_changed": False,
        "counts_for_formal_validation": False,
    }


def hybrid_exploratory_candidate_names(queue_run: str) -> set[str]:
    """Return candidates that later formal hybrid validation must exclude."""
    batches = frappe.get_all(
        BATCH_DOCTYPE,
        filters={"queue_run": queue_run, "status": ["in", list(ACTIVE_BATCH_STATUSES)]},
        pluck="name",
        limit_page_length=10_000,
    )
    if not batches:
        return set()
    return {
        str(name)
        for name in frappe.get_all(
            ITEM_DOCTYPE,
            filters={"exploratory_batch": ["in", batches]},
            pluck="review_candidate",
            limit_page_length=100_000,
        )
    }
