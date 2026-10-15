"""Guarded Development-only lower-score hybrid identity automation.

The authorization comes from a completed Hybrid Prospective Validation and
its independently reviewed blinded holdout.  Selection is limited to untouched
isolated pairs matching the frozen corroboration rule and source scope.  Heavy
identity writes run as restart-safe background batches.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from typing import Any, Iterable

import frappe
from frappe.utils import now_datetime

from db_connector.api_hybrid_validation import (
    RUN_PURPOSE,
    _hybrid_inventory,
    _provenance_reasons,
    _request_context,
)
from db_connector.api_identity_resolution import (
    _append_event,
    _current_memberships,
    materialization_enabled,
    materialize_identity,
    preview_materialization,
)
from db_connector.fuzzy_matching.automation import current_shared_group, rolling_qc_summary
from db_connector.fuzzy_matching.hybrid_automation import bounded_hybrid_pairs
from db_connector.fuzzy_matching.hybrid_research import HYBRID_RESEARCH_RULE_VERSION
from db_connector.fuzzy_matching.identity import expected_identity_fingerprints


RUN_DOCTYPE = "CCD Match Evaluation Run"
QUEUE_DOCTYPE = "CCD Match Review Queue Run"
CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
BATCH_DOCTYPE = "CCD Splink Automation Batch"
ITEM_DOCTYPE = "CCD Splink Automation Batch Item"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
HYBRID_BATCH_TYPE = "Development Hybrid Automation"
HYBRID_CHANNEL = "Hybrid Development"
FINAL_QC_STATUSES = {"Agreed", "Adjudicated"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _lock_rows(doctype: str, names: Iterable[str]) -> None:
    ordered = tuple(sorted({str(name) for name in names if str(name)}))
    if not ordered:
        return
    placeholders = ", ".join(["%s"] * len(ordered))
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name IN ({placeholders}) "
        "ORDER BY name FOR UPDATE",
        ordered,
    )


def _lock_settings() -> None:
    frappe.db.sql(
        "SELECT field FROM `tabSingles` WHERE doctype = %s ORDER BY field FOR UPDATE",
        (SETTINGS_DOCTYPE,),
    )


def _set_settings(values: dict[str, Any]) -> None:
    for fieldname, value in values.items():
        frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)


def _as_bool(value: Any) -> bool:
    return str(value or "0").strip().casefold() in {"1", "true", "yes", "on"}


def _load_json_list(value: Any) -> tuple[str, ...]:
    try:
        loaded = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(loaded, (list, tuple)):
        return ()
    return tuple(sorted({str(item) for item in loaded if str(item)}))


def _evidence_blockers(run: Any) -> list[str]:
    blockers: list[str] = []
    if str(run.run_purpose or "") != RUN_PURPOSE or not run.hybrid_development_only:
        blockers.append("wrong_or_non_development_run")
        return blockers
    if str(run.status or "") != "Completed" or str(run.approval_status or "") != "Approved":
        blockers.append("prospective_validation_not_approved")
    if str(run.hybrid_holdout_status or "") != "Completed":
        blockers.append("hybrid_holdout_not_completed")
    if str(run.hybrid_second_approval_status or "") != "Approved":
        blockers.append("hybrid_holdout_second_approval_missing")
    if run.authorization_stale:
        blockers.append("authorization_marked_stale")
    try:
        metrics = json.loads(run.hybrid_holdout_metrics_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        metrics = {}
    if not (metrics.get("combined_authorization_gate") or {}).get("passed"):
        blockers.append("combined_precision_gate_missing_or_failed")
    blockers.extend(_provenance_reasons(run))
    return sorted(set(blockers))


def _authorization_blockers(settings: Any, run: Any) -> list[str]:
    blockers = _evidence_blockers(run)
    expected = {
        "authorized_hybrid_validation_run": str(run.name),
        "authorized_hybrid_queue": str(run.validation_queue_run or ""),
        "authorized_hybrid_policy": str(run.matching_policy or ""),
        "authorized_hybrid_rule_version": str(run.hybrid_rule_version or ""),
        "authorized_hybrid_fingerprint": str(run.authorization_fingerprint or ""),
    }
    for fieldname, value in expected.items():
        if str(getattr(settings, fieldname, "") or "") != value:
            blockers.append(fieldname + "_mismatch")
    if abs(
        float(settings.authorized_hybrid_minimum_score or 0)
        - float(run.hybrid_minimum_score or 0)
    ) > 1e-12:
        blockers.append("authorized_hybrid_minimum_score_mismatch")
    if abs(
        float(settings.authorized_hybrid_upper_cutoff or 0)
        - float(run.frozen_automatic_cutoff or 0)
    ) > 1e-12:
        blockers.append("authorized_hybrid_upper_cutoff_mismatch")
    if _load_json_list(settings.authorized_hybrid_source_scope_json) != _load_json_list(
        run.source_scope_json
    ):
        blockers.append("authorized_hybrid_source_scope_mismatch")
    if _load_json_list(settings.authorized_hybrid_route_scope_json) != _load_json_list(
        run.hybrid_route_scope_json
    ):
        blockers.append("authorized_hybrid_route_scope_mismatch")
    return sorted(set(blockers))


@frappe.whitelist(methods=["POST"])
def authorize_hybrid_development_automation(
    run_name: str, reason: str, confirm_run_name: str
) -> dict[str, Any]:
    """Adopt approved evidence into default-off Development controls."""
    _require_manager()
    if str(confirm_run_name or "").strip() != str(run_name or "").strip():
        frappe.throw("Type the exact Hybrid Validation Run ID to confirm")
    if not str(reason or "").strip():
        frappe.throw("An authorization reason is required")
    _lock_rows(RUN_DOCTYPE, (run_name,))
    _lock_settings()
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    blockers = _evidence_blockers(run)
    if blockers:
        frappe.throw("Hybrid Development authorization is blocked: " + ", ".join(blockers))
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    revision = int(settings.hybrid_automation_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Approve",
        reason="development_hybrid_automation_authorization:" + str(reason).strip(),
        nonce=hashlib.sha256(
            f"hybrid-authorization\x1f{run.name}\x1f{run.authorization_fingerprint}\x1f{revision}".encode()
        ).hexdigest(),
        from_status="Unconfigured",
        to_status="Authorized but Disabled",
        metadata={
            "channel": HYBRID_CHANNEL,
            "development_only": True,
            "validation_run": run.name,
            "authorization_fingerprint": run.authorization_fingerprint,
        },
    )
    _set_settings(
        {
            "automatic_hybrid_enabled": 0,
            "automatic_hybrid_scheduling_enabled": 0,
            "hybrid_automation_paused": 0,
            "hybrid_pause_reason": None,
            "hybrid_automation_control_revision": revision,
            "authorized_hybrid_validation_run": run.name,
            "authorized_hybrid_queue": run.validation_queue_run,
            "authorized_hybrid_policy": run.matching_policy,
            "authorized_hybrid_minimum_score": run.hybrid_minimum_score,
            "authorized_hybrid_upper_cutoff": run.frozen_automatic_cutoff,
            "authorized_hybrid_rule_version": run.hybrid_rule_version,
            "authorized_hybrid_source_scope_json": run.source_scope_json,
            "authorized_hybrid_route_scope_json": run.hybrid_route_scope_json,
            "authorized_hybrid_fingerprint": run.authorization_fingerprint,
            "hybrid_automation_authorization_event": event,
            "last_hybrid_automation_status": "Authorized but Disabled",
            "last_hybrid_automation_error": None,
        }
    )
    frappe.db.commit()
    return {
        "run": run.name,
        "status": "Authorized but Disabled",
        "event": event,
        "development_only": True,
    }


@frappe.whitelist(methods=["POST"])
def set_automatic_hybrid(
    enabled: int | str, reason: str, confirm_phrase: str
) -> dict[str, Any]:
    _require_manager()
    desired = _as_bool(enabled)
    exact = (
        "ENABLE DEVELOPMENT HYBRID AUTOMATION"
        if desired
        else "DISABLE DEVELOPMENT HYBRID AUTOMATION"
    )
    if str(confirm_phrase or "").strip() != exact:
        frappe.throw(f"Type {exact} exactly to confirm")
    if not str(reason or "").strip():
        frappe.throw("A control-change reason is required")
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    run_name = str(settings.authorized_hybrid_validation_run or "")
    if desired:
        if not settings.materialization_enabled:
            frappe.throw("Live Identity Materialization must be enabled")
        if not settings.automatic_qc_assignment_enabled:
            frappe.throw("Automatic QC Assignment must be enabled")
        if settings.hybrid_automation_paused:
            frappe.throw("Resume the Hybrid Development circuit breaker first")
        if not run_name or not frappe.db.exists(RUN_DOCTYPE, run_name):
            frappe.throw("Authorize approved Hybrid Development evidence first")
        blockers = _authorization_blockers(settings, frappe.get_doc(RUN_DOCTYPE, run_name))
        if blockers:
            frappe.throw("Hybrid Development automation cannot be enabled: " + ", ".join(blockers))
    revision = int(settings.hybrid_automation_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Enable" if desired else "Disable",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(
            f"hybrid-control\x1f{revision}\x1f{desired}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Enabled" if settings.automatic_hybrid_enabled else "Disabled",
        to_status="Enabled" if desired else "Disabled",
        metadata={
            "channel": HYBRID_CHANNEL,
            "development_only": True,
            "validation_run": run_name,
            "revision": revision,
        },
    )
    values = {
        "automatic_hybrid_enabled": int(desired),
        "hybrid_automation_control_revision": revision,
        "last_hybrid_automation_status": "Enabled" if desired else "Disabled",
        "last_hybrid_automation_error": None,
    }
    if not desired:
        values["automatic_hybrid_scheduling_enabled"] = 0
    _set_settings(values)
    frappe.db.commit()
    return {"status": "Enabled" if desired else "Disabled", "event": event}


@frappe.whitelist(methods=["POST"])
def set_hybrid_scheduling(
    enabled: int | str, reason: str, confirm_phrase: str
) -> dict[str, Any]:
    _require_manager()
    desired = _as_bool(enabled)
    exact = (
        "ENABLE SCHEDULED DEVELOPMENT HYBRID"
        if desired
        else "DISABLE SCHEDULED DEVELOPMENT HYBRID"
    )
    if str(confirm_phrase or "").strip() != exact:
        frappe.throw(f"Type {exact} exactly to confirm")
    if not str(reason or "").strip():
        frappe.throw("A scheduling control reason is required")
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if desired and not settings.automatic_hybrid_enabled:
        frappe.throw("Enable Development Hybrid Automation before its daily schedule")
    revision = int(settings.hybrid_automation_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Enable" if desired else "Disable",
        reason="development_hybrid_schedule:" + str(reason).strip(),
        nonce=hashlib.sha256(
            f"hybrid-schedule\x1f{revision}\x1f{desired}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Scheduled" if settings.automatic_hybrid_scheduling_enabled else "Manual Only",
        to_status="Scheduled" if desired else "Manual Only",
        metadata={"channel": HYBRID_CHANNEL, "development_only": True},
    )
    _set_settings(
        {
            "automatic_hybrid_scheduling_enabled": int(desired),
            "hybrid_automation_control_revision": revision,
        }
    )
    frappe.db.commit()
    return {"status": "Scheduled" if desired else "Manual Only", "event": event}


def _pause_hybrid(
    reason: str, *, candidate: str = "", investigation: str = ""
) -> dict[str, Any]:
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.hybrid_automation_paused:
        return {
            "already_paused": True,
            "event": str(settings.hybrid_automation_authorization_event or ""),
        }
    revision = int(settings.hybrid_automation_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Pause",
        reason=str(reason),
        nonce=hashlib.sha256(
            f"hybrid-pause\x1f{revision}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Monitoring",
        to_status="Paused",
        metadata={
            "channel": HYBRID_CHANNEL,
            "candidate": candidate,
            "investigation": investigation,
            "development_only": True,
        },
    )
    _set_settings(
        {
            "hybrid_automation_paused": 1,
            "hybrid_pause_reason": str(reason)[:140],
            "automatic_hybrid_scheduling_enabled": 0,
            "hybrid_automation_control_revision": revision,
            "last_hybrid_automation_status": "Paused",
            "last_hybrid_automation_error": str(reason)[:140],
        }
    )
    return {"already_paused": False, "event": event, "revision": revision}


@frappe.whitelist(methods=["POST"])
def pause_hybrid_automation(reason: str, confirm_phrase: str) -> dict[str, Any]:
    _require_manager()
    if str(confirm_phrase or "").strip() != "PAUSE DEVELOPMENT HYBRID AUTOMATION":
        frappe.throw("Type PAUSE DEVELOPMENT HYBRID AUTOMATION exactly to confirm")
    if not str(reason or "").strip():
        frappe.throw("A pause reason is required")
    result = _pause_hybrid("manager_pause:" + str(reason).strip())
    frappe.db.commit()
    return {"status": "Paused", **result}


@frappe.whitelist(methods=["POST"])
def resume_hybrid_automation(reason: str, confirm_phrase: str) -> dict[str, Any]:
    _require_manager()
    if str(confirm_phrase or "").strip() != "RESUME DEVELOPMENT HYBRID AUTOMATION":
        frappe.throw("Type RESUME DEVELOPMENT HYBRID AUTOMATION exactly to confirm")
    if not str(reason or "").strip():
        frappe.throw("A resume reason is required")
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.automatic_hybrid_enabled:
        frappe.throw("Disable Development Hybrid Automation before clearing its breaker")
    blockers: list[str] = []
    if frappe.db.count(
        INVESTIGATION_DOCTYPE,
        {"automation_channel": HYBRID_CHANNEL, "status": "Open"},
    ):
        blockers.append("open_hybrid_investigations")
    overdue_rows = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={
            "automation_channel": HYBRID_CHANNEL,
            "automated_qc_due_at": ["<", now_datetime()],
            "automated_qc_stale": 0,
            "automated_qc_review_status": ["not in", ["Agreed", "Adjudicated", "Stale"]],
        },
        pluck="name",
        limit_page_length=1,
    )
    if overdue_rows:
        blockers.append("overdue_hybrid_qc")
    if blockers:
        frappe.throw("Hybrid Development automation cannot resume: " + ", ".join(blockers))
    revision = int(settings.hybrid_automation_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=SETTINGS_DOCTYPE,
        entity_name=SETTINGS_DOCTYPE,
        event_type="Resume",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(
            f"hybrid-resume\x1f{revision}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Paused",
        to_status="Monitoring",
        metadata={"channel": HYBRID_CHANNEL, "development_only": True},
    )
    _set_settings(
        {
            "hybrid_automation_paused": 0,
            "hybrid_pause_reason": None,
            "hybrid_automation_control_revision": revision,
            "last_hybrid_automation_status": "Monitoring",
            "last_hybrid_automation_error": None,
        }
    )
    frappe.db.commit()
    return {"status": "Monitoring", "event": event, "revision": revision}


def _candidate_preview(run: Any, row: dict[str, Any]) -> dict[str, Any]:
    left = str(row["left_record"])
    right = str(row["right_record"])
    expected_modified = {
        left: str(row.get("left_modified_at") or ""),
        right: str(row.get("right_modified_at") or ""),
    }
    fingerprints = expected_identity_fingerprints(
        (
            (left, row.get("left_identity_fingerprint")),
            (right, row.get("right_identity_fingerprint")),
        )
    )
    preview = preview_materialization(
        origin="Hybrid Automated",
        origin_doctype=CANDIDATE_DOCTYPE,
        origin_document=str(row["name"]),
        policy_snapshot_json=run.policy_snapshot_json,
        record_ids=[left, right],
        groups=[[left, right]],
        exclusions=[],
        expected_fingerprints=fingerprints,
        expected_modified=expected_modified,
    )
    metadata = row["component_metadata"]
    return {
        "component_fingerprint": str(metadata["component_fingerprint"]),
        "component_size": 2,
        "complete_clique": True,
        "candidate_names": [str(row["name"])],
        "record_ids": [left, right],
        "eligible": bool(preview["safe"]),
        "already_applied": bool(preview["already_applied"]),
        "error": ",".join(preview["conflicts"]),
        "hybrid_primary_route": str(row.get("primary_route") or ""),
        "hybrid_routes": list(row.get("routes") or ()),
        "hybrid_score_band": str(row.get("score_band") or ""),
        "hybrid_birthday_status": str(row.get("birthday_status") or ""),
        "probabilistic_score": float(row.get("probabilistic_score") or 0),
        "source_pair": str(row.get("source_pair") or ""),
        "hybrid_rule_version": str(run.hybrid_rule_version or ""),
    }


def _automatic_hybrid_selection(settings: Any, run: Any) -> dict[str, Any]:
    batch, queue, _splink_settings, floor, cutoff = _request_context(
        run.hybrid_exploratory_batch
    )
    inventory = _hybrid_inventory(batch, queue, floor, cutoff)
    # The governed setting is itself the per-cycle bound. Do not silently
    # clamp it to 100: larger Development runs remain finite because selection
    # cannot exceed the frozen eligible inventory, and apply commits each
    # component independently.
    limit = max(int(settings.automatic_hybrid_components_per_run or 1), 1)
    allowed_sources = _load_json_list(run.source_scope_json)
    scoped = bounded_hybrid_pairs(
        inventory["rows"],
        component_limit=max(len(inventory["rows"]), 1),
        allowed_sources=allowed_sources,
    )
    planned: list[dict[str, Any]] = []
    preview_exclusions: dict[str, int] = {}
    for row in scoped:
        item = _candidate_preview(run, row)
        if not item["eligible"]:
            for reason in str(item["error"] or "").split(","):
                if reason:
                    preview_exclusions[reason] = preview_exclusions.get(reason, 0) + 1
            continue
        planned.append(item)
        if len(planned) >= limit:
            break
    return {
        "planned": planned,
        "summary": {
            "validation_run": str(run.name),
            "queue_run": str(queue.name),
            "development_only": True,
            "zero_write": True,
            "minimum_score": float(run.hybrid_minimum_score or 0),
            "upper_cutoff": float(run.frozen_automatic_cutoff or 0),
            "hybrid_rule_version": str(run.hybrid_rule_version or ""),
            "source_scope": list(allowed_sources),
            "route_scope": list(_load_json_list(run.hybrid_route_scope_json)),
            "component_limit": limit,
            "scanned_candidate_count": int(inventory["scanned_count"]),
            "rule_matching_edge_count": int(inventory["rule_matching_edge_count"]),
            "eligible_component_count": len(scoped),
            "selected_component_count": len(planned),
            "selected_candidate_count": len(planned),
            "selected_candidates": [item["candidate_names"][0] for item in planned],
            "selected_by_route": _count(planned, "hybrid_primary_route"),
            "selected_by_score_band": _count(planned, "hybrid_score_band"),
            "selected_by_source": _count(planned, "source_pair"),
            "inventory_exclusions": inventory["exclusion_counts"],
            "preview_exclusions": dict(sorted(preview_exclusions.items())),
        },
    }


def _count(rows: Iterable[dict[str, Any]], fieldname: str) -> dict[str, int]:
    output: dict[str, int] = {}
    for row in rows:
        key = str(row.get(fieldname) or "Unknown")
        output[key] = output.get(key, 0) + 1
    return dict(sorted(output.items()))


@frappe.whitelist()
def preview_automatic_hybrid_cycle() -> dict[str, Any]:
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    operational_blockers: list[str] = []
    if not settings.automatic_hybrid_enabled:
        operational_blockers.append("automatic_hybrid_disabled")
    if not settings.materialization_enabled:
        operational_blockers.append("master_materialization_disabled")
    if not settings.automatic_qc_assignment_enabled:
        operational_blockers.append("automatic_qc_assignment_disabled")
    if settings.hybrid_automation_paused:
        operational_blockers.append("hybrid_automation_paused")
    run_name = str(settings.authorized_hybrid_validation_run or "")
    summary = {
        "validation_run": run_name,
        "development_only": True,
        "zero_write": True,
        "component_limit": int(settings.automatic_hybrid_components_per_run or 1),
        "eligible_component_count": 0,
        "selected_component_count": 0,
        "selected_candidate_count": 0,
        "selected_candidates": [],
        "selected_by_route": {},
        "selected_by_score_band": {},
        "selected_by_source": {},
        "inventory_exclusions": {},
        "preview_exclusions": {},
    }
    authorization_blockers: list[str] = []
    if not run_name or not frappe.db.exists(RUN_DOCTYPE, run_name):
        authorization_blockers.append("authorized_hybrid_validation_missing")
        approved = frappe.get_all(
            RUN_DOCTYPE,
            filters={
                "run_purpose": RUN_PURPOSE,
                "status": "Completed",
                "approval_status": "Approved",
                "hybrid_holdout_status": "Completed",
                "hybrid_second_approval_status": "Approved",
                "hybrid_development_only": 1,
            },
            fields=["name"],
            order_by="creation desc",
            limit_page_length=1,
        )
        if approved:
            run = frappe.get_doc(RUN_DOCTYPE, approved[0].name)
            evidence_blockers = _evidence_blockers(run)
            authorization_blockers.extend(evidence_blockers)
            if not evidence_blockers:
                summary = _automatic_hybrid_selection(settings, run)["summary"]
    else:
        run = frappe.get_doc(RUN_DOCTYPE, run_name)
        authorization_blockers = _authorization_blockers(settings, run)
        if not authorization_blockers:
            summary = _automatic_hybrid_selection(settings, run)["summary"]
    blockers = sorted(set(operational_blockers + authorization_blockers))
    return {
        **summary,
        "authorization_blockers": authorization_blockers,
        "operational_blockers": blockers,
        "would_queue_now": not blockers and bool(summary["selected_component_count"]),
    }


def run_automatic_hybrid_cycle(*, requested_by: str = "Administrator") -> dict[str, Any]:
    from db_connector.api_splink_automation import _create_batch

    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if not settings.automatic_hybrid_enabled:
        return {"status": "Disabled", "selected_components": 0}
    if settings.hybrid_automation_paused:
        return {
            "status": "Paused",
            "selected_components": 0,
            "reason": settings.hybrid_pause_reason,
        }
    run_name = str(settings.authorized_hybrid_validation_run or "")
    if not run_name or not frappe.db.exists(RUN_DOCTYPE, run_name):
        return {"status": "Not Authorized", "selected_components": 0}
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    blockers = _authorization_blockers(settings, run)
    if blockers:
        pause = _pause_hybrid("automatic_preflight:" + ",".join(blockers))
        frappe.db.commit()
        return {
            "status": "Paused",
            "selected_components": 0,
            "blockers": blockers,
            **pause,
        }
    selection = _automatic_hybrid_selection(settings, run)
    planned = selection["planned"]
    summary = selection["summary"]
    if not planned:
        return {"status": "No Eligible Work", "selected_components": 0, **summary}
    _lock_rows(
        CANDIDATE_DOCTYPE,
        (name for item in planned for name in item["candidate_names"]),
    )
    batch = _create_batch(
        batch_type=HYBRID_BATCH_TYPE,
        run=run,
        planned=planned,
        queue_run=str(run.validation_queue_run),
    )
    batch.db_set(
        {
            "status": "Queued",
            "approved_at": now_datetime(),
            "approved_by": requested_by,
        },
        update_modified=False,
    )
    frappe.enqueue(
        "db_connector.api_hybrid_automation.apply_hybrid_batch",
        queue="long",
        timeout=7_200,
        enqueue_after_commit=True,
        job_id=f"ccd-hybrid-batch-{batch.name}",
        deduplicate=True,
        batch_name=batch.name,
        requested_by=requested_by,
    )
    frappe.db.commit()
    return {
        "status": "Queued",
        "batch": batch.name,
        "selected_components": len(planned),
        **summary,
    }


@frappe.whitelist(methods=["POST"])
def run_automatic_hybrid_now() -> dict[str, Any]:
    _require_manager()
    return run_automatic_hybrid_cycle(requested_by=str(frappe.session.user))


@frappe.whitelist()
def get_hybrid_batch_status(batch_name: str) -> dict[str, Any]:
    """Return the live batch state used by the Desk progress dialog."""
    _require_manager()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if str(batch.batch_type or "") != HYBRID_BATCH_TYPE:
        frappe.throw("This is not a Development Hybrid Automation batch")
    return {
        "batch": str(batch.name),
        "status": str(batch.status or ""),
        "selected_components": int(batch.component_count or 0),
        "applied": int(batch.applied_count or 0),
        "exceptions": int(batch.exception_count or 0),
        "stale": int(batch.stale_count or 0),
        "error": str(batch.error_summary or ""),
        "applied_at": batch.applied_at,
    }


@frappe.whitelist(methods=["POST"])
def retry_hybrid_batch(
    batch_name: str, reason: str, confirm_batch_name: str
) -> dict[str, Any]:
    _require_manager()
    if str(confirm_batch_name or "").strip() != str(batch_name or "").strip():
        frappe.throw("Type the exact Hybrid Batch ID to confirm retry")
    if not str(reason or "").strip():
        frappe.throw("A retry reason is required")
    _lock_rows(BATCH_DOCTYPE, (batch_name,))
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.batch_type != HYBRID_BATCH_TYPE:
        frappe.throw("This is not a Development Hybrid Automation batch")
    if batch.status in {"Queued", "Applying", "Applied", "Applied with Exceptions"}:
        return {"batch": batch.name, "status": batch.status, "already_queued": True}
    if batch.status != "Failed":
        frappe.throw("Only a failed Development Hybrid batch may be retried")
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
    blockers = _authorization_blockers(settings, run)
    if blockers or str(batch.authorization_fingerprint) != str(run.authorization_fingerprint):
        frappe.throw("The Development Hybrid authorization is no longer current")
    frappe.db.set_value(
        BATCH_DOCTYPE,
        batch.name,
        {"status": "Queued", "error_summary": None},
        update_modified=False,
    )
    _append_event(
        entity_doctype=BATCH_DOCTYPE,
        entity_name=batch.name,
        event_type="Retry",
        reason=str(reason).strip(),
        nonce=hashlib.sha256(
            f"hybrid-batch-retry\x1f{batch.name}\x1f{reason}".encode()
        ).hexdigest(),
        from_status="Failed",
        to_status="Queued",
        metadata={"channel": HYBRID_CHANNEL, "development_only": True},
    )
    frappe.enqueue(
        "db_connector.api_hybrid_automation.apply_hybrid_batch",
        queue="long",
        timeout=7_200,
        enqueue_after_commit=True,
        job_id=f"ccd-hybrid-batch-{batch.name}",
        deduplicate=True,
        batch_name=batch.name,
        requested_by=frappe.session.user,
    )
    frappe.db.commit()
    return {"batch": batch.name, "status": "Queued"}


def _materialize_hybrid_item(batch: Any, item: Any, candidate: Any) -> dict[str, Any]:
    run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
    left = str(candidate.left_record)
    right = str(candidate.right_record)
    fingerprints = expected_identity_fingerprints(
        (
            (left, candidate.left_identity_fingerprint),
            (right, candidate.right_identity_fingerprint),
        )
    )
    policy_snapshot_sha256 = str(
        frappe.db.get_value(QUEUE_DOCTYPE, run.validation_queue_run, "policy_snapshot_sha256")
        or ""
    )
    return materialize_identity(
        origin="Hybrid Automated",
        origin_doctype=CANDIDATE_DOCTYPE,
        origin_document=candidate.name,
        policy_snapshot_json=run.policy_snapshot_json,
        policy_snapshot_sha256=policy_snapshot_sha256,
        matching_policy=run.matching_policy,
        record_ids=[left, right],
        groups=[[left, right]],
        exclusions=[],
        expected_fingerprints=fingerprints,
        expected_modified={
            left: str(candidate.left_modified_at or ""),
            right: str(candidate.right_modified_at or ""),
        },
        reason_codes=[
            "validated_lower_score_hybrid_automation",
            str(candidate.hybrid_automation_route or "frozen_corroboration_route"),
            str(candidate.hybrid_automation_score_band or "frozen_score_band"),
        ],
        review_context={
            "validation_run": run.name,
            "automation_batch": batch.name,
            "component_fingerprint": item.component_fingerprint,
            "candidate_names": [str(candidate.name)],
            "hybrid_rule_version": str(run.hybrid_rule_version or ""),
            "minimum_score": float(run.hybrid_minimum_score or 0),
            "upper_cutoff": float(run.frozen_automatic_cutoff or 0),
            "development_only": True,
        },
        is_demonstration=True,
    )


def apply_hybrid_batch(batch_name: str, requested_by: str) -> dict[str, Any]:
    """Apply a frozen hybrid batch component-by-component and restart safely."""
    if "System Manager" not in set(frappe.get_roles(requested_by)):
        raise frappe.PermissionError("System Manager role is required")
    previous_user = frappe.session.user
    frappe.set_user(requested_by)
    try:
        _lock_rows(BATCH_DOCTYPE, (batch_name,))
        batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
        if batch.batch_type != HYBRID_BATCH_TYPE:
            frappe.throw("This is not a Development Hybrid Automation batch")
        if batch.status in {"Applied", "Applied with Exceptions"}:
            return {"batch": batch.name, "status": batch.status, "already_applied": True}
        if batch.status == "Applying":
            return {"batch": batch.name, "status": "Applying", "already_running": True}
        if batch.status not in {"Queued", "Failed"}:
            frappe.throw("Only an approved queued Hybrid batch may be applied")
        settings = frappe.get_single(SETTINGS_DOCTYPE)
        if (
            not settings.automatic_hybrid_enabled
            or not settings.automatic_qc_assignment_enabled
            or not materialization_enabled(automated=True, channel="hybrid")
        ):
            frappe.throw("Hybrid automation, QC assignment, or live materialization is disabled")
        run = frappe.get_doc(RUN_DOCTYPE, batch.validation_run)
        blockers = _authorization_blockers(settings, run)
        if blockers or str(batch.authorization_fingerprint) != str(run.authorization_fingerprint):
            frappe.throw(
                "The Development Hybrid authorization is no longer current: "
                + ",".join(blockers or ["batch_authorization_mismatch"])
            )
        batch.db_set("status", "Applying", update_modified=False)
        frappe.db.commit()
        applied = exceptions = stale = 0
        for item in batch.items:
            if item.status in {"Applied", "Already Applied"}:
                applied += 1
                continue
            if item.status == "Ineligible":
                exceptions += 1
                continue
            savepoint = f"hybrid_item_{int(item.idx)}"
            frappe.db.savepoint(savepoint)
            try:
                names = json.loads(item.candidate_names_json or "[]")
                if len(names) != 1:
                    frappe.throw("Development Hybrid automation currently requires isolated pairs")
                _lock_rows(CANDIDATE_DOCTYPE, names)
                candidate = frappe.get_doc(CANDIDATE_DOCTYPE, names[0])
                if (
                    str(candidate.automation_batch or "") != str(batch.name)
                    or str(candidate.automation_channel or "") != HYBRID_CHANNEL
                ):
                    frappe.throw("The candidate is not reserved by this Hybrid batch")
                result = _materialize_hybrid_item(batch, item, candidate)
                item_status = (
                    "Already Applied" if result["status"] == "Already Applied" else "Applied"
                )
                frappe.db.set_value(
                    ITEM_DOCTYPE,
                    item.name,
                    {
                        "status": item_status,
                        "identity_decisions_json": _json([result.get("identity_decision")]),
                        "identity_groups_json": _json(result.get("identity_groups") or []),
                        "error_code": None,
                    },
                    update_modified=False,
                )
                frappe.db.set_value(
                    CANDIDATE_DOCTYPE,
                    candidate.name,
                    {
                        "automation_status": "Applied",
                        "identity_decision": result.get("identity_decision"),
                        "identity_groups_json": _json(result.get("identity_groups") or []),
                        "materialization_status": "Applied",
                        "automated_applied_at": now_datetime(),
                        # This is an eligible population, not a mandatory QC
                        # backlog.  The shared weekly allocator deterministically
                        # selects a bounded sample and closes every other case as
                        # Not Selected without assigning an SLA.
                        "automated_qc_review_status": "Sampling Eligible",
                        "automated_qc_assigned_at": None,
                        "automated_qc_due_at": None,
                    },
                    update_modified=False,
                )
                applied += 1
            except Exception as exc:
                frappe.db.rollback(save_point=savepoint)
                code = f"{type(exc).__name__}:{str(exc)}"[:140]
                is_stale = any(
                    marker in str(exc)
                    for marker in (
                        "identity_fingerprint_changed",
                        "source_modified_after_snapshot",
                        "frozen_identity_snapshot_incomplete",
                    )
                )
                frappe.db.set_value(
                    ITEM_DOCTYPE,
                    item.name,
                    {"status": "Stale" if is_stale else "Exception", "error_code": code},
                    update_modified=False,
                )
                for name in json.loads(item.candidate_names_json or "[]"):
                    frappe.db.set_value(
                        CANDIDATE_DOCTYPE,
                        name,
                        {
                            "automation_status": "Stale" if is_stale else "Exception",
                            "automation_ineligibility_reason": code,
                        },
                        update_modified=False,
                    )
                if is_stale:
                    stale += 1
                else:
                    exceptions += 1
            frappe.db.commit()
        status = "Applied" if not exceptions and not stale else "Applied with Exceptions"
        frappe.db.set_value(
            BATCH_DOCTYPE,
            batch.name,
            {
                "status": status,
                "applied_count": applied,
                "exception_count": exceptions,
                "stale_count": stale,
                "applied_at": now_datetime(),
                "applied_by": requested_by,
            },
            update_modified=False,
        )
        _set_settings(
            {
                "last_hybrid_automation_run_at": now_datetime(),
                "last_hybrid_automation_batch": batch.name,
                "last_hybrid_automation_status": status,
                "last_hybrid_automation_error": None,
            }
        )
        frappe.db.commit()
        try:
            from db_connector.api_splink_automation import run_shared_review_capacity

            qc_sampling = run_shared_review_capacity()
        except Exception as sampling_exc:
            # Materialization is already component-atomically committed.  A
            # transient allocator failure must leave the candidates Sampling
            # Eligible for the next monitor rather than misreport the applied
            # identity batch as Failed.
            frappe.db.rollback()
            frappe.log_error(
                traceback.format_exc(),
                "Development Hybrid QC sampling deferred",
            )
            qc_sampling = {
                "status": "Deferred",
                "error": f"{type(sampling_exc).__name__}:{str(sampling_exc)}"[:140],
            }
        return {
            "batch": batch.name,
            "status": status,
            "applied": applied,
            "exceptions": exceptions,
            "stale": stale,
            "qc_sampling": qc_sampling,
        }
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            BATCH_DOCTYPE,
            batch_name,
            {"status": "Failed", "error_summary": f"{type(exc).__name__}:{str(exc)}"[:140]},
            update_modified=False,
        )
        _set_settings(
            {
                "last_hybrid_automation_status": "Failed",
                "last_hybrid_automation_error": f"{type(exc).__name__}:{str(exc)}"[:140],
            }
        )
        frappe.log_error(traceback.format_exc(), "Development Hybrid automation batch failed")
        frappe.db.commit()
        raise
    finally:
        frappe.set_user(previous_user)


def apply_hybrid_qc_breaker(candidate: Any) -> dict[str, Any]:
    """Pause only the Hybrid channel after a finalized QC Different."""
    if candidate.automated_qc_failure_action:
        return {"status": "Already Applied", "action": candidate.automated_qc_failure_action}
    memberships = _current_memberships(
        (str(candidate.left_record), str(candidate.right_record))
    )
    shared = current_shared_group(
        (dict(row) for row in memberships),
        str(candidate.left_record),
        str(candidate.right_record),
    )
    key = hashlib.sha256(f"hybrid-qc\x1f{candidate.name}".encode()).hexdigest()
    investigation = frappe.db.get_value(
        INVESTIGATION_DOCTYPE, {"investigation_key": key}, "name"
    )
    if not investigation:
        investigation = frappe.get_doc(
            {
                "doctype": INVESTIGATION_DOCTYPE,
                "investigation_key": key,
                "automation_channel": HYBRID_CHANNEL,
                "splink_candidate": candidate.name,
                "splink_validation_run": candidate.automation_validation_run,
                "identity_decision": candidate.identity_decision or None,
                "identity_group": shared or None,
                "pause_scope": f"hybrid:{candidate.source_pair}",
                "status": "Open",
                "reason": f"confirmed_hybrid_qc_different:{candidate.name}",
                "opened_at": now_datetime(),
                "opened_by": frappe.session.user or "Administrator",
            }
        ).insert(ignore_permissions=True).name
    pause = _pause_hybrid(
        f"confirmed_hybrid_qc_different:{candidate.name}",
        candidate=candidate.name,
        investigation=str(investigation),
    )
    suspended = 0
    if shared:
        from db_connector.api_identity_qc import _suspend_group_for_qc

        suspended = _suspend_group_for_qc(str(shared), str(investigation))
    action = (
        f"hybrid_paused;investigation={investigation};shared_group={shared or 'none'};"
        f"memberships_needing_revalidation={suspended};pause_event={pause.get('event', '')}"
    )
    candidate.db_set("automated_qc_failure_action", action, update_modified=False)
    return {
        "status": "Paused",
        "investigation": investigation,
        "identity_group": shared or "",
        "suspended": suspended,
    }


def monitor_hybrid_and_run_scheduled(*, allow_schedule: bool = True) -> dict[str, Any]:
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    result: dict[str, Any] = {"status": "Monitoring"}
    now = now_datetime()
    overdue = frappe.db.count(
        CANDIDATE_DOCTYPE,
        {
            "automation_channel": HYBRID_CHANNEL,
            "automated_qc_due_at": ["<", now],
            "automated_qc_stale": 0,
            "automated_qc_review_status": ["not in", ["Agreed", "Adjudicated", "Stale"]],
        },
    )
    if overdue:
        result["pause"] = _pause_hybrid(f"overdue_hybrid_qc:{overdue}")
    finalized = frappe.get_all(
        CANDIDATE_DOCTYPE,
        filters={
            "automation_channel": HYBRID_CHANNEL,
            "automated_qc_review_status": ["in", ["Agreed", "Adjudicated"]],
        },
        fields=["name", "automated_qc_final_label"],
        order_by="automated_qc_finalized_at asc, name asc",
        limit_page_length=10_000,
    )
    excluded = set(
        str(name)
        for name in frappe.get_all(
            INVESTIGATION_DOCTYPE,
            filters={
                "automation_channel": HYBRID_CHANNEL,
                "status": "Resolved",
                "resolution_action": "QC Review Error",
            },
            pluck="splink_candidate",
            limit_page_length=10_000,
        )
        if name
    )
    rolling = rolling_qc_summary(
        [
            str(row.automated_qc_final_label)
            for row in finalized
            if str(row.name) not in excluded
        ],
        max(int(settings.rolling_qc_window or 100), 1),
    )
    result["rolling_qc"] = rolling
    if rolling["window_complete"] and rolling["wilson_95"][0] < 0.95:
        result["pause"] = _pause_hybrid(
            f"hybrid_rolling_precision_below_0.95:{rolling['wilson_95'][0]:.6f}"
        )
    frappe.db.commit()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.automatic_hybrid_scheduling_enabled and allow_schedule:
        result["automatic_hybrid"] = run_automatic_hybrid_cycle()
    elif settings.automatic_hybrid_scheduling_enabled:
        result["automatic_hybrid"] = {
            "status": "Deferred Until Refreshed Identity State",
            "selected_components": 0,
        }
    else:
        result["automatic_hybrid"] = {
            "status": "Manual Only",
            "selected_components": 0,
        }
    result["status"] = "Completed"
    return result


@frappe.whitelist()
def get_hybrid_automation_dashboard() -> dict[str, Any]:
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    approved = frappe.get_all(
        RUN_DOCTYPE,
        filters={
            "run_purpose": RUN_PURPOSE,
            "status": "Completed",
            "approval_status": "Approved",
            "hybrid_holdout_status": "Completed",
            "hybrid_second_approval_status": "Approved",
            "hybrid_development_only": 1,
        },
        fields=["name"],
        order_by="creation desc",
        limit_page_length=1,
    )
    latest = str(approved[0].name) if approved else ""
    authorized = str(settings.authorized_hybrid_validation_run or "")
    blockers: list[str] = []
    if authorized and frappe.db.exists(RUN_DOCTYPE, authorized):
        blockers = _authorization_blockers(
            settings, frappe.get_doc(RUN_DOCTYPE, authorized)
        )
    elif authorized:
        blockers = ["authorized_hybrid_validation_missing"]
    return {
        "available": bool(latest or authorized),
        "latest_approved_run": latest,
        "authorized_run": authorized,
        "can_authorize": bool(latest and latest != authorized),
        "enabled": bool(settings.automatic_hybrid_enabled),
        "scheduling_enabled": bool(settings.automatic_hybrid_scheduling_enabled),
        "paused": bool(settings.hybrid_automation_paused),
        "pause_reason": str(settings.hybrid_pause_reason or ""),
        "component_limit": int(settings.automatic_hybrid_components_per_run or 1),
        "last_run_at": settings.last_hybrid_automation_run_at,
        "last_batch": str(settings.last_hybrid_automation_batch or ""),
        "last_status": str(settings.last_hybrid_automation_status or ""),
        "last_error": str(settings.last_hybrid_automation_error or ""),
        "authorization_blockers": blockers,
        "development_only": True,
    }
