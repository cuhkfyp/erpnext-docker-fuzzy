"""Bounded unattended recovery of overlap-only identity exceptions.

The service consumes only previously authorized Tiered High or ultra-high
Splink seeds.  It expands every active identity group touched by a seed,
replaces the complete closure atomically, and leaves hard conflicts as
visible exceptions.  QC is a deterministic, non-blocking post-apply sample.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from typing import Any, Iterable

import frappe
from frappe.utils import add_days, now_datetime

from db_connector.api_identity_overlap import (
    COMPONENT_DOCTYPE,
    SPLINK_BATCH_DOCTYPE,
    SPLINK_ITEM_DOCTYPE,
    _preview,
    apply_combined_component_resolution,
    automatic_overlap_error_is_eligible,
)
from db_connector.api_identity_resolution import _append_event


SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
BATCH_DOCTYPE = "CCD Automatic Overlap Batch"
ITEM_DOCTYPE = "CCD Automatic Overlap Batch Item"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
VALIDATION_DOCTYPE = "CCD Tiered One-to-Many Validation"
FINAL_COMPONENT_STATUSES = {"Agreed", "Adjudicated"}
TERMINAL_BATCH_STATUSES = {"Applied", "Applied with Exceptions", "Failed"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _as_bool(value: Any) -> bool:
    return str(value or "0").strip().casefold() in {"1", "true", "yes", "on"}


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_reviewer() -> None:
    if not (
        {"System Manager", "CCD Match Reviewer", "CCD Match Sensitive Reviewer"}
        & set(frappe.get_roles())
    ):
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _set_settings(values: dict[str, Any]) -> None:
    for fieldname, value in values.items():
        frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)


def _lock_settings() -> None:
    frappe.db.sql(
        "SELECT field FROM `tabSingles` WHERE doctype = %s ORDER BY field FOR UPDATE",
        (SETTINGS_DOCTYPE,),
    )


def _source_seeds(settings: Any) -> list[tuple[str, str, str]]:
    """Return deterministic Tiered-first overlap-only source exceptions."""
    seeds: list[tuple[str, str, str]] = []
    component_rows = frappe.get_all(
        COMPONENT_DOCTYPE,
        filters={
            "stale": 0,
            "materialization_status": "Exception",
            "tiered_automation_status": "Exception",
        },
        fields=["name", "materialization_error"],
        order_by="name",
        limit_page_length=100_000,
    )
    seeds.extend(
        ("Tiered High", COMPONENT_DOCTYPE, str(row.name))
        for row in component_rows
        if automatic_overlap_error_is_eligible(row.materialization_error)
    )

    run_name = str(settings.authorized_splink_validation_run or "")
    parent_names = (
        frappe.get_all(
            SPLINK_BATCH_DOCTYPE,
            filters={
                "batch_type": "Scheduled Splink",
                "validation_run": run_name,
                "status": ["in", ["Applied with Exceptions", "Applied", "Applying"]],
            },
            pluck="name",
            order_by="creation, name",
            limit_page_length=100_000,
        )
        if run_name
        else []
    )
    if parent_names:
        item_rows = frappe.get_all(
            SPLINK_ITEM_DOCTYPE,
            filters={"parent": ["in", parent_names], "status": "Exception"},
            fields=["name", "error_code"],
            order_by="parent, idx, name",
            limit_page_length=100_000,
        )
        seeds.extend(
            ("Ultra-High Splink", SPLINK_ITEM_DOCTYPE, str(row.name))
            for row in item_rows
            if automatic_overlap_error_is_eligible(row.error_code)
        )
    return seeds


def _safe_preview(channel: str, seed_doctype: str, seed_document: str) -> dict[str, Any]:
    try:
        result = _preview(
            seed_doctype,
            seed_document,
            automatic_overlap=True,
        )
        return {
            "channel": channel,
            "seed_doctype": seed_doctype,
            "seed_document": seed_document,
            "preview": result,
            "error": "",
        }
    except Exception as exc:
        frappe.db.rollback()
        return {
            "channel": channel,
            "seed_doctype": seed_doctype,
            "seed_document": seed_document,
            "preview": None,
            "error": f"{type(exc).__name__}:{str(exc)[:500]}",
        }


def _inventory(settings: Any) -> list[dict[str, Any]]:
    return [_safe_preview(*seed) for seed in _source_seeds(settings)]


def _select(
    inventory: Iterable[dict[str, Any]],
    limit: int,
    *,
    require_controls: bool,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    selected: list[dict[str, Any]] = []
    selected_records: set[str] = set()
    reasons: Counter[str] = Counter()
    for item in inventory:
        preview = item.get("preview")
        if not preview:
            reasons[item.get("error") or "preview_failed"] += 1
            continue
        hard = tuple(str(value) for value in preview.get("hard_conflicts") or ())
        if hard:
            reasons.update(hard)
            continue
        controls = tuple(str(value) for value in preview.get("control_blockers") or ())
        if require_controls and controls:
            reasons.update(controls)
            continue
        records = {str(row["record_id"]) for row in preview.get("records") or ()}
        if records.intersection(selected_records):
            reasons["overlaps_component_already_selected_this_cycle"] += 1
            continue
        if len(selected) >= limit:
            reasons["beyond_cycle_component_cap"] += 1
            continue
        selected.append(item)
        selected_records.update(records)
    return selected, reasons


def _preview_summary(*, require_controls: bool = False) -> dict[str, Any]:
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    inventory = _inventory(settings)
    limit = max(int(settings.automatic_overlap_components_per_run or 10), 1)
    selected, excluded = _select(
        inventory,
        limit,
        require_controls=require_controls,
    )
    hard_eligible = sum(
        bool(row.get("preview")) and not row["preview"].get("hard_conflicts")
        for row in inventory
    )
    channel_available = Counter(str(row["channel"]) for row in inventory)
    channel_eligible = Counter(
        str(row["channel"])
        for row in inventory
        if row.get("preview") and not row["preview"].get("hard_conflicts")
    )
    channel_selected = Counter(str(row["channel"]) for row in selected)
    control_blockers = Counter(
        str(blocker)
        for row in inventory
        if row.get("preview") and not row["preview"].get("hard_conflicts")
        for blocker in row["preview"].get("control_blockers") or ()
    )
    if not bool(settings.materialization_enabled):
        control_blockers["live_identity_materialization_disabled"] += 1
    details = []
    for row in selected:
        preview = row["preview"]
        details.append(
            {
                "channel": row["channel"],
                "seed_doctype": row["seed_doctype"],
                "seed_document": row["seed_document"],
                "initial_record_count": len(
                    (preview.get("included_pending_scopes") or [{}])[0].get(
                        "records", ()
                    )
                ),
                "expanded_record_count": len(preview.get("records") or ()),
                "records": [
                    str(item["record_id"]) for item in preview.get("records") or ()
                ],
                "scope_fingerprint": str(preview["scope_fingerprint"]),
                "replacement_groups": preview["replacement_groups"],
                "warnings": preview.get("warnings") or [],
                "control_blockers": preview.get("control_blockers") or [],
            }
        )
    return {
        "zero_write": True,
        "automatic_overlap_enabled": bool(settings.automatic_overlap_enabled),
        "live_materialization_enabled": bool(settings.materialization_enabled),
        "maximum_components": limit,
        "maximum_scope_records": int(settings.maximum_automatic_overlap_records or 10),
        "qc_sample_percent": float(settings.automatic_overlap_qc_percent or 10),
        "source_exception_count": len(inventory),
        "structurally_eligible_count": int(hard_eligible),
        "selected_component_count": len(selected),
        "available_by_channel": dict(sorted(channel_available.items())),
        "eligible_by_channel": dict(sorted(channel_eligible.items())),
        "selected_by_channel": dict(sorted(channel_selected.items())),
        "excluded_reason_counts": dict(sorted(excluded.items())),
        "control_blocker_counts": dict(sorted(control_blockers.items())),
        "selected_components": details,
        "would_queue_now": bool(selected) and not control_blockers,
        "_selected": selected,
    }


@frappe.whitelist()
def preview_automatic_overlap_cycle() -> dict[str, Any]:
    """Preview the exact deterministic closure selection without identity writes."""
    _require_manager()
    result = _preview_summary(require_controls=False)
    return {key: value for key, value in result.items() if key != "_selected"}


@frappe.whitelist(methods=["POST"])
def set_automatic_overlap(
    enabled: int | str,
    reason: str,
    confirm_phrase: str,
) -> dict[str, Any]:
    """Govern the separate default-off closure recovery control."""
    _require_manager()
    desired = _as_bool(enabled)
    exact = (
        "ENABLE AUTOMATIC OVERLAP RECOVERY"
        if desired
        else "DISABLE AUTOMATIC OVERLAP RECOVERY"
    )
    if str(confirm_phrase or "").strip() != exact:
        frappe.throw(f"Type {exact} exactly")
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        frappe.throw("A control-change reason is required")

    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if desired:
        if not settings.materialization_enabled:
            frappe.throw("Enable Live Identity Materialization first")
        if not (
            settings.automatic_tiered_one_to_many_enabled
            or settings.automatic_splink_enabled
        ):
            frappe.throw(
                "Enable Tiered One-to-Many or Automatic Splink first"
            )
    current = bool(settings.automatic_overlap_enabled)
    if current == desired:
        return {
            "status": "Enabled" if desired else "Disabled",
            "event": str(settings.automatic_overlap_authorization_event or ""),
            "unchanged": True,
        }
    revision = int(settings.automation_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Enable" if desired else "Disable",
        reason=normalized_reason,
        nonce=_sha(
            {
                "control": "automatic_complete_group_overlap_recovery",
                "revision": revision,
                "enabled": desired,
                "reason": normalized_reason,
            }
        ),
        from_status="Enabled" if current else "Disabled",
        to_status="Enabled" if desired else "Disabled",
        metadata={
            "channel": "Automatic Complete-Group Overlap Recovery",
            "revision": revision,
            "tiered_validation": str(settings.tiered_one_to_many_validation or ""),
            "splink_validation": str(settings.authorized_splink_validation_run or ""),
            "birthday_conflicts_are_hard": False,
            "complete_validated_hkid_conflicts_are_hard": True,
            "post_apply_qc_is_non_blocking": True,
        },
    )
    _set_settings(
        {
            "automatic_overlap_enabled": int(desired),
            "automatic_overlap_authorization_event": event if desired else None,
            "automation_control_revision": revision,
            "last_automatic_overlap_status": "Enabled" if desired else "Disabled",
            "last_automatic_overlap_error": None,
        }
    )
    frappe.db.commit()
    return {
        "status": "Enabled" if desired else "Disabled",
        "event": event,
        "revision": revision,
    }


def _create_batch(summary: dict[str, Any]) -> Any:
    selected = summary["_selected"]
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    manifest = [
        {
            "channel": row["channel"],
            "seed_doctype": row["seed_doctype"],
            "seed_document": row["seed_document"],
            "scope_fingerprint": row["preview"]["scope_fingerprint"],
            "replacement_groups": row["preview"]["replacement_groups"],
        }
        for row in selected
    ]
    selection_sha = _sha(manifest)
    idempotency_key = _sha(
        {
            "selection": selection_sha,
            "authorization_event": str(settings.automatic_overlap_authorization_event or ""),
            "maximum_components": int(settings.automatic_overlap_components_per_run or 10),
            "maximum_scope_records": int(settings.maximum_automatic_overlap_records or 10),
            "qc_sample_percent": float(settings.automatic_overlap_qc_percent or 10),
        }
    )
    existing = frappe.db.get_value(
        BATCH_DOCTYPE,
        {"idempotency_key": idempotency_key},
        ["name", "status"],
        as_dict=True,
    )
    if existing:
        return frappe.get_doc(BATCH_DOCTYPE, existing.name)

    public_summary = {
        key: value for key, value in summary.items() if key != "_selected"
    }
    batch = frappe.get_doc(
        {
            "doctype": BATCH_DOCTYPE,
            "status": "Queued",
            "selection_manifest_sha256": selection_sha,
            "idempotency_key": idempotency_key,
            "authorization_event": settings.automatic_overlap_authorization_event,
            "tiered_validation": settings.tiered_one_to_many_validation or None,
            "splink_validation": settings.authorized_splink_validation_run or None,
            "maximum_components": int(settings.automatic_overlap_components_per_run or 10),
            "maximum_scope_records": int(settings.maximum_automatic_overlap_records or 10),
            "qc_sample_percent": float(settings.automatic_overlap_qc_percent or 10),
            "selected_component_count": len(selected),
            "tiered_component_count": sum(
                row["channel"] == "Tiered High" for row in selected
            ),
            "splink_component_count": sum(
                row["channel"] == "Ultra-High Splink" for row in selected
            ),
            "previewed_at": now_datetime(),
            "previewed_by": frappe.session.user,
            "requested_by": frappe.session.user,
            "preview_json": _json(public_summary),
        }
    )
    for row in selected:
        preview = row["preview"]
        included = preview.get("included_pending_scopes") or [{}]
        batch.append(
            "items",
            {
                "channel": row["channel"],
                "seed_doctype": row["seed_doctype"],
                "seed_document": row["seed_document"],
                "status": "Planned",
                "initial_record_count": len(included[0].get("records") or ()),
                "expanded_record_count": len(preview.get("records") or ()),
                "scope_fingerprint": preview["scope_fingerprint"],
                "replacement_groups_json": _json(preview["replacement_groups"]),
                "warnings_json": _json(preview.get("warnings") or []),
                "hard_conflicts_json": _json(preview.get("hard_conflicts") or []),
            },
        )
    try:
        batch.insert(ignore_permissions=True)
        return batch
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        existing_name = frappe.db.get_value(
            BATCH_DOCTYPE, {"idempotency_key": idempotency_key}, "name"
        )
        if not existing_name:
            raise
        return frappe.get_doc(BATCH_DOCTYPE, existing_name)


def _start_cycle(*, requested_by: str) -> dict[str, Any]:
    summary = _preview_summary(require_controls=True)
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    blockers = []
    if not settings.automatic_overlap_enabled:
        blockers.append("automatic_overlap_disabled")
    if not settings.materialization_enabled:
        blockers.append("live_identity_materialization_disabled")
    if blockers:
        frappe.throw("Automatic overlap recovery is blocked: " + ", ".join(blockers))
    if not summary["selected_component_count"]:
        return {
            "status": "No Eligible Components",
            **{key: value for key, value in summary.items() if key != "_selected"},
        }

    batch = _create_batch(summary)
    if str(batch.status) in TERMINAL_BATCH_STATUSES:
        return {
            "status": str(batch.status),
            "batch": str(batch.name),
            "existing": True,
            "selected_component_count": int(batch.selected_component_count or 0),
        }
    _set_settings(
        {
            "last_automatic_overlap_batch": batch.name,
            "last_automatic_overlap_status": "Queued",
            "last_automatic_overlap_error": None,
        }
    )
    frappe.db.commit()
    frappe.enqueue(
        "db_connector.api_automatic_overlap.apply_automatic_overlap_batch",
        queue="long",
        timeout=21_600,
        enqueue_after_commit=True,
        job_id=f"ccd-automatic-overlap-{batch.name}",
        batch_name=batch.name,
        requested_by=requested_by,
    )
    return {
        "status": "Queued",
        "batch": batch.name,
        "existing": False,
        "selected_component_count": int(batch.selected_component_count or 0),
        "tiered_component_count": int(batch.tiered_component_count or 0),
        "splink_component_count": int(batch.splink_component_count or 0),
    }


@frappe.whitelist(methods=["POST"])
def start_automatic_overlap_cycle() -> dict[str, Any]:
    _require_manager()
    return _start_cycle(requested_by=str(frappe.session.user))


def _refresh_counts(batch: Any) -> dict[str, int]:
    statuses = [str(row.status or "") for row in batch.items]
    counts = {
        "applied": sum(status in {"Applied", "No Change"} for status in statuses),
        "exceptions": sum(status == "Exception" for status in statuses),
        "stale": sum(status == "Stale" for status in statuses),
        "pending": sum(status in {"Planned", "Applying"} for status in statuses),
    }
    batch.db_set(
        {
            "applied_component_count": counts["applied"],
            "exception_component_count": counts["exceptions"],
            "stale_component_count": counts["stale"],
        },
        update_modified=False,
    )
    return counts


def _candidate_names_for_splink_item(seed_name: str) -> list[str]:
    raw = frappe.db.get_value(SPLINK_ITEM_DOCTYPE, seed_name, "candidate_names_json")
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return sorted(str(value) for value in values if str(value))


def _deterministic_sample(items: Iterable[Any], batch_name: str, count: int) -> list[Any]:
    ranked = sorted(
        items,
        key=lambda item: (
            hashlib.sha256(
                f"automatic-overlap-qc\x1f{batch_name}\x1f{item.name}".encode()
            ).hexdigest(),
            str(item.name),
        ),
    )
    return ranked[: max(int(count), 0)]


def _assign_post_apply_qc(batch: Any) -> dict[str, int]:
    """Assign only available shared capacity; never roll back identity writes."""
    from db_connector.api_splink_automation import _weekly_assigned_count

    applied = [row for row in batch.items if str(row.status) in {"Applied", "No Change"}]
    reviewable: list[Any] = []
    for row in applied:
        if str(row.channel) == "Tiered High":
            review_status = frappe.db.get_value(
                COMPONENT_DOCTYPE, row.seed_document, "review_status"
            )
            if str(review_status or "") not in FINAL_COMPONENT_STATUSES:
                reviewable.append(row)
            continue
        candidates = _candidate_names_for_splink_item(str(row.seed_document))
        if any(
            (
                state := frappe.db.get_value(
                    CANDIDATE_DOCTYPE,
                    candidate_name,
                    ["automated_qc_assigned_at", "automated_qc_review_status"],
                    as_dict=True,
                )
            )
            and not state.automated_qc_assigned_at
            and str(state.automated_qc_review_status or "")
            not in {"Agreed", "Adjudicated"}
            for candidate_name in candidates
        ):
            reviewable.append(row)
    requested = (
        max(1, math.ceil(len(reviewable) * float(batch.qc_sample_percent or 0) / 100))
        if reviewable
        else 0
    )
    remaining = max(20 - int(_weekly_assigned_count()), 0)
    assigned_target = min(requested, remaining)
    sampled = _deterministic_sample(reviewable, str(batch.name), assigned_target)
    sampled_names = {str(row.name) for row in sampled}
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    assigned_at = now_datetime()
    due_at = add_days(assigned_at, int(settings.qc_sla_days or 14))
    assigned = 0

    for row in applied:
        if str(row.channel) == "Ultra-High Splink":
            candidates = _candidate_names_for_splink_item(str(row.seed_document))
            candidate_states = {
                candidate_name: frappe.db.get_value(
                    CANDIDATE_DOCTYPE,
                    candidate_name,
                    ["automated_qc_assigned_at", "automated_qc_review_status"],
                    as_dict=True,
                )
                for candidate_name in candidates
            }
            available_candidates = [
                candidate_name
                for candidate_name in candidates
                if candidate_states.get(candidate_name)
                and not candidate_states[candidate_name].automated_qc_assigned_at
                and str(
                    candidate_states[candidate_name].automated_qc_review_status or ""
                ) not in {"Agreed", "Adjudicated"}
            ]
            selected_candidate = (
                available_candidates[0]
                if str(row.name) in sampled_names and available_candidates
                else ""
            )
            for candidate_name in candidates:
                current = candidate_states.get(candidate_name)
                if not current:
                    continue
                if candidate_name == selected_candidate:
                    frappe.db.set_value(
                        CANDIDATE_DOCTYPE,
                        candidate_name,
                        {
                            "automated_qc_review_status": "Unreviewed",
                            "automated_qc_assigned_at": assigned_at,
                            "automated_qc_due_at": due_at,
                            "automation_status": "QC Assigned",
                        },
                        update_modified=False,
                    )
                    assigned += 1
                elif not current.automated_qc_assigned_at and str(
                    current.automated_qc_review_status or ""
                ) not in {"Agreed", "Adjudicated"}:
                    frappe.db.set_value(
                        CANDIDATE_DOCTYPE,
                        candidate_name,
                        {
                            "automated_qc_review_status": "Not Selected",
                            "automation_status": "Applied",
                        },
                        update_modified=False,
                    )
        elif str(row.name) in sampled_names:
            review = frappe.get_doc(COMPONENT_DOCTYPE, row.seed_document)
            if str(review.review_status or "") in FINAL_COMPONENT_STATUSES:
                # A completed independent component review is stronger than a
                # newly assigned sample and consumes no fresh weekly capacity.
                review.db_set(
                    "tiered_automation_status", "QC Complete", update_modified=False
                )
                continue
            review.db_set(
                {
                    "tiered_governance_cohort": "Ongoing QC",
                    "tiered_automation_status": "QC Selected",
                    "tiered_qc_assigned_at": assigned_at,
                    "tiered_qc_due_at": due_at,
                },
                update_modified=False,
            )
            assigned += 1

    unassigned = max(requested - assigned, 0)
    batch.db_set(
        {
            "qc_requested_count": requested,
            "qc_assigned_count": assigned,
            "qc_unassigned_count": unassigned,
        },
        update_modified=False,
    )
    if requested:
        _append_event(
            entity_doctype=BATCH_DOCTYPE,
            entity_name=batch.name,
            event_type="QC Assign",
            reason="non_blocking_post_apply_overlap_qc_sample",
            nonce=_sha(
                {
                    "batch": batch.name,
                    "requested": requested,
                    "assigned": assigned,
                    "unassigned": unassigned,
                }
            ),
            from_status=f"Requested {requested}",
            to_status=f"Assigned {assigned}",
            metadata={
                "requested_qc": requested,
                "assigned_qc": assigned,
                "risk_accepted_unassigned_qc": unassigned,
                "shared_weekly_capacity_remaining_before_assignment": remaining,
                "identity_writes_are_not_capacity_gated": True,
            },
        )
    return {"requested": requested, "assigned": assigned, "unassigned": unassigned}


def _apply_automatic_overlap_batch(
    batch_name: str,
    requested_by: str = "Administrator",
) -> dict[str, Any]:
    """Apply one immutable batch; every component commits independently."""
    frappe.set_user(str(requested_by or "Administrator"))
    _require_manager()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if str(batch.status) in TERMINAL_BATCH_STATUSES:
        return get_automatic_overlap_batch_progress(batch.name)
    if _sha(
        [
            {
                "channel": str(row.channel),
                "seed_doctype": str(row.seed_doctype),
                "seed_document": str(row.seed_document),
                "scope_fingerprint": str(row.scope_fingerprint),
                "replacement_groups": json.loads(row.replacement_groups_json or "[]"),
            }
            for row in batch.items
        ]
    ) != str(batch.selection_manifest_sha256):
        frappe.throw("The frozen automatic overlap batch manifest is corrupt")

    batch.db_set(
        {"status": "Applying", "started_at": now_datetime()},
        update_modified=False,
    )
    _set_settings(
        {
            "last_automatic_overlap_batch": batch.name,
            "last_automatic_overlap_status": "Applying",
            "last_automatic_overlap_error": None,
        }
    )
    frappe.db.commit()

    for item in batch.items:
        if str(item.status) in {"Applied", "No Change"}:
            continue
        item.db_set("status", "Applying", update_modified=False)
        frappe.db.commit()
        try:
            fresh = _preview(
                str(item.seed_doctype),
                str(item.seed_document),
                automatic_overlap=True,
            )
            if fresh["scope_fingerprint"] != str(item.scope_fingerprint):
                item.db_set(
                    {
                        "status": "Stale",
                        "error_code": "expanded_scope_changed_after_batch_freeze",
                        "hard_conflicts_json": _json(fresh.get("hard_conflicts") or []),
                    },
                    update_modified=False,
                )
                frappe.db.commit()
                continue
            if fresh.get("hard_conflicts"):
                item.db_set(
                    {
                        "status": "Exception",
                        "error_code": "hard_conflict:" + ",".join(fresh["hard_conflicts"]),
                        "hard_conflicts_json": _json(fresh["hard_conflicts"]),
                    },
                    update_modified=False,
                )
                frappe.db.commit()
                continue
            result = apply_combined_component_resolution(
                seed_doctype=str(item.seed_doctype),
                seed_document=str(item.seed_document),
                replacement_groups_json=item.replacement_groups_json,
                expected_scope_fingerprint=str(item.scope_fingerprint),
                reason=(
                    "Authorized automatic complete-group overlap recovery; "
                    "birthday disagreement and missing or partial identifiers are non-hard evidence"
                ),
                confirm_seed_document=str(item.seed_document),
                confirm_safety_warnings=1,
                automatic_overlap=1,
            )
            status = str(result.get("status") or "Applied")
            item.db_set(
                {
                    "status": "No Change" if status == "No Change" else "Applied",
                    "resolution": result.get("resolution") or None,
                    "error_code": None,
                },
                update_modified=False,
            )
            frappe.db.commit()
        except Exception as exc:
            frappe.db.rollback()
            item = frappe.get_doc(ITEM_DOCTYPE, item.name)
            error = f"{type(exc).__name__}:{str(exc)[:1000]}"
            stale = any(
                marker in error
                for marker in (
                    "scope changed",
                    "fingerprint_changed",
                    "source_modified_after_snapshot",
                    "no longer",
                    "authorization changed",
                )
            )
            item.db_set(
                {"status": "Stale" if stale else "Exception", "error_code": error},
                update_modified=False,
            )
            frappe.db.commit()
        batch = frappe.get_doc(BATCH_DOCTYPE, batch.name)
        _refresh_counts(batch)
        frappe.db.commit()

    batch = frappe.get_doc(BATCH_DOCTYPE, batch.name)
    counts = _refresh_counts(batch)
    qc = _assign_post_apply_qc(batch)
    status = (
        "Applied"
        if not counts["exceptions"] and not counts["stale"]
        else "Applied with Exceptions"
    )
    batch.db_set(
        {"status": status, "completed_at": now_datetime()},
        update_modified=False,
    )
    _set_settings(
        {
            "last_automatic_overlap_batch": batch.name,
            "last_automatic_overlap_status": status,
            "last_automatic_overlap_error": (
                f"{counts['exceptions']} exception(s), {counts['stale']} stale"
                if counts["exceptions"] or counts["stale"]
                else None
            ),
        }
    )
    frappe.db.commit()
    return {
        "batch": batch.name,
        "status": status,
        **counts,
        "qc": qc,
    }


def apply_automatic_overlap_batch(
    batch_name: str,
    requested_by: str = "Administrator",
) -> dict[str, Any]:
    """Visible fail-closed wrapper for the long-queue worker."""
    try:
        return _apply_automatic_overlap_batch(batch_name, requested_by)
    except Exception as exc:
        frappe.db.rollback()
        error = f"{type(exc).__name__}:{str(exc)[:1000]}"
        if frappe.db.exists(BATCH_DOCTYPE, batch_name):
            frappe.db.set_value(
                BATCH_DOCTYPE,
                batch_name,
                {
                    "status": "Failed",
                    "completed_at": now_datetime(),
                    "error_summary": error,
                },
                update_modified=False,
            )
            _set_settings(
                {
                    "last_automatic_overlap_batch": batch_name,
                    "last_automatic_overlap_status": "Failed",
                    "last_automatic_overlap_error": error,
                }
            )
            frappe.db.commit()
        raise


@frappe.whitelist()
def get_automatic_overlap_batch_progress(batch_name: str) -> dict[str, Any]:
    _require_reviewer()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    items = [
        {
            "name": str(row.name),
            "channel": str(row.channel or ""),
            "seed_doctype": str(row.seed_doctype or ""),
            "seed_document": str(row.seed_document or ""),
            "status": str(row.status or ""),
            "initial_records": int(row.initial_record_count or 0),
            "expanded_records": int(row.expanded_record_count or 0),
            "resolution": str(row.resolution or ""),
            "error": str(row.error_code or ""),
        }
        for row in batch.items
    ]
    return {
        "batch": str(batch.name),
        "status": str(batch.status or ""),
        "selected": int(batch.selected_component_count or 0),
        "tiered": int(batch.tiered_component_count or 0),
        "splink": int(batch.splink_component_count or 0),
        "applied": int(batch.applied_component_count or 0),
        "exceptions": int(batch.exception_component_count or 0),
        "stale": int(batch.stale_component_count or 0),
        "qc_requested": int(batch.qc_requested_count or 0),
        "qc_assigned": int(batch.qc_assigned_count or 0),
        "qc_unassigned": int(batch.qc_unassigned_count or 0),
        "error": str(batch.error_summary or ""),
        "items": items,
    }


def monitor_and_run_automatic_overlap() -> dict[str, Any]:
    """Daily best-effort cycle, after Tiered/Splink refreshed identity state."""
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if not settings.automatic_overlap_enabled:
        return {"status": "Disabled", "selected_component_count": 0}
    open_batches = frappe.get_all(
        BATCH_DOCTYPE,
        filters={"status": ["in", ["Queued", "Applying"]]},
        pluck="name",
        limit=1,
    )
    if open_batches:
        return {
            "status": "Already Running",
            "batch": str(open_batches[0]),
            "selected_component_count": 0,
        }
    return _start_cycle(requested_by="Administrator")
