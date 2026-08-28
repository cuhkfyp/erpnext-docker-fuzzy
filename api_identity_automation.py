"""Default-off, bounded unattended Tiered Evidence materialization."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import get_datetime, now_datetime

from db_connector.api_identity_activation import (
    _apply_activation_batch,
    approve_activation_batch,
    create_automatic_activation_batch,
    preview_automatic_component_selection,
)
from db_connector.api_identity_qc import (
    SETTINGS_DOCTYPE,
    _append_control_event,
    _lock_settings,
    _pause_automation,
    _require_manager,
    _set_single_values,
)

RUN_DOCTYPE = "CCD Match Canary Run"
BATCH_DOCTYPE = "CCD Identity Activation Batch"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
DEFAULT_AUTOMATIC_COMPONENT_LIMIT = 10


def _configuration_blockers(settings: Any, *, require_enabled: bool) -> list[str]:
    blockers = []
    if require_enabled and not settings.automatic_tiered_enabled:
        blockers.append("automatic_tiered_disabled")
    if not settings.materialization_enabled:
        blockers.append("master_materialization_disabled")
    if settings.automation_paused:
        blockers.append("tiered_circuit_breaker_paused")
    if not settings.automatic_qc_assignment_enabled:
        blockers.append("automatic_qc_assignment_disabled")
    if not settings.automatic_tiered_canary:
        blockers.append("authorized_canary_missing")
    if not settings.automatic_tiered_policy:
        blockers.append("authorized_policy_missing")
    elif frappe.db.get_value(
        "CCD Matching Policy", settings.automatic_tiered_policy, "status"
    ) != "Pilot":
        blockers.append("authorized_policy_not_pilot")
    if not settings.automatic_tiered_authorization_event:
        blockers.append("automation_authorization_event_missing")
    limit = int(
        settings.automatic_tiered_components_per_run
        or DEFAULT_AUTOMATIC_COMPONENT_LIMIT
    )
    if limit < 1 or limit > 100:
        blockers.append("automatic_component_limit_out_of_range")
    if settings.automatic_tiered_canary:
        canary = frappe.db.get_value(
            RUN_DOCTYPE,
            settings.automatic_tiered_canary,
            [
                "status",
                "matching_policy",
                "qc_last_assignment_at",
                "qc_next_assignment_at",
                "qc_overdue_count",
            ],
            as_dict=True,
        )
        if not canary:
            blockers.append("authorized_canary_missing")
        else:
            if canary.status not in {"Ready", "Active"}:
                blockers.append(f"authorized_canary_not_ready:{canary.status}")
            if str(canary.matching_policy or "") != str(
                settings.automatic_tiered_policy or ""
            ):
                blockers.append("authorized_policy_canary_mismatch")
            if not canary.qc_last_assignment_at:
                blockers.append("qc_cadence_never_started")
            elif canary.qc_next_assignment_at and get_datetime(
                canary.qc_next_assignment_at
            ) < now_datetime():
                blockers.append("qc_assignment_cadence_overdue")
            if int(canary.qc_overdue_count or 0):
                blockers.append(f"overdue_qc_cases:{int(canary.qc_overdue_count)}")
    open_investigations = frappe.db.count(INVESTIGATION_DOCTYPE, {"status": "Open"})
    if open_investigations:
        blockers.append(f"open_qc_investigations:{open_investigations}")
    return sorted(set(blockers))


def _update_last_run(
    *, status: str, batch: str = "", error: str = "", commit: bool = True
) -> None:
    _set_single_values(
        {
            "last_automatic_run_at": now_datetime(),
            "last_automatic_batch": batch or None,
            "last_automatic_status": str(status)[:140],
            "last_automatic_error": str(error or "")[:140] or None,
        }
    )
    if commit:
        frappe.db.commit()


@frappe.whitelist()
def preview_automatic_tiered_run() -> dict[str, Any]:
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    blockers = _configuration_blockers(settings, require_enabled=False)
    selection: dict[str, Any] = {
        "run": str(settings.automatic_tiered_canary or ""),
        "zero_write": True,
        "selected_component_count": 0,
        "selected_recommendation_count": 0,
        "planned_identity_group_count": 0,
        "planned_membership_count": 0,
        "skipped_unsafe_component_count": 0,
        "components": [],
        "skipped_components": [],
    }
    scope_ready = bool(
        settings.automatic_tiered_canary
        and frappe.db.exists(RUN_DOCTYPE, settings.automatic_tiered_canary)
    )
    if scope_ready:
        try:
            selection = preview_automatic_component_selection(
                str(settings.automatic_tiered_canary),
                int(
                    settings.automatic_tiered_components_per_run
                    or DEFAULT_AUTOMATIC_COMPONENT_LIMIT
                ),
            )
        except Exception as exc:
            blockers.append(f"selection_preview_failed:{type(exc).__name__}:{str(exc)}")
    return {
        **selection,
        "zero_write": True,
        "automatic_tiered_enabled": bool(settings.automatic_tiered_enabled),
        "materialization_enabled": bool(settings.materialization_enabled),
        "automation_paused": bool(settings.automation_paused),
        "automatic_qc_assignment_enabled": bool(
            settings.automatic_qc_assignment_enabled
        ),
        "control_revision": int(settings.automation_control_revision or 0),
        "operational_blockers": sorted(set(blockers)),
        "would_write_now": not blockers and bool(settings.automatic_tiered_enabled),
    }


@frappe.whitelist()
def set_automatic_tiered_enabled(
    enabled: int | str, reason: str, confirm_settings_name: str
) -> dict[str, Any]:
    _require_manager()
    if str(confirm_settings_name or "").strip() != SETTINGS_DOCTYPE:
        frappe.throw("Type the exact Settings ID to confirm this control change")
    reason = str(reason or "").strip()
    if not reason:
        frappe.throw("A control-change reason is required")
    desired = str(enabled or "0").strip().casefold() in {"1", "true", "yes", "on"}
    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if desired:
        blockers = [
            item
            for item in _configuration_blockers(settings, require_enabled=False)
            if item != "automation_authorization_event_missing"
        ]
        if blockers:
            frappe.throw(
                "Automatic Tiered cannot be enabled: " + ", ".join(blockers)
            )
    revision = int(settings.automation_control_revision or 0) + 1
    _set_single_values(
        {
            "automatic_tiered_enabled": int(desired),
            "automation_control_revision": revision,
        }
    )
    event = _append_control_event(
        event_type="Enable" if desired else "Disable",
        action="automatic_tiered_materialization",
        revision=revision,
        reason=reason,
        metadata={
            "from_status": "Enabled" if settings.automatic_tiered_enabled else "Disabled",
            "to_status": "Enabled" if desired else "Disabled",
            "canary": str(settings.automatic_tiered_canary or ""),
            "policy": str(settings.automatic_tiered_policy or ""),
            "component_limit": int(
                settings.automatic_tiered_components_per_run
                or DEFAULT_AUTOMATIC_COMPONENT_LIMIT
            ),
        },
    )
    _set_single_values(
        {
            "automatic_tiered_authorization_event": event if desired else None,
            "last_automatic_status": "Enabled" if desired else "Disabled",
            "last_automatic_error": None,
        }
    )
    frappe.db.commit()
    return {
        "status": "Enabled" if desired else "Disabled",
        "event": event,
        "revision": revision,
    }


def _execute_cycle() -> dict[str, Any]:
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if not settings.automatic_tiered_enabled:
        return {"status": "Disabled", "batch": ""}
    blockers = _configuration_blockers(settings, require_enabled=True)
    if blockers:
        if "automatic_qc_assignment_disabled" in blockers:
            _pause_automation(
                "global",
                "automatic_qc_assignment_disabled",
                metadata={"source": "automatic_tiered_cycle"},
            )
        _update_last_run(status="Blocked", error=", ".join(blockers))
        return {"status": "Blocked", "batch": "", "blockers": blockers}

    frozen_revision = int(settings.automation_control_revision or 0)
    run_name = str(settings.automatic_tiered_canary)
    created = create_automatic_activation_batch(
        run_name,
        int(
            settings.automatic_tiered_components_per_run
            or DEFAULT_AUTOMATIC_COMPONENT_LIMIT
        ),
        frozen_revision,
        str(settings.automatic_tiered_authorization_event),
    )
    batch_name = str(created.get("batch") or "")
    if not batch_name:
        _update_last_run(status="No Eligible Components")
        return {
            "status": "No Eligible Components",
            "batch": "",
            "skipped_unsafe_component_count": created.get(
                "skipped_unsafe_component_count", 0
            ),
        }

    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if not batch.is_automatic or int(batch.automation_control_revision or 0) != frozen_revision:
        frappe.throw("The frozen automatic batch authorization is stale")
    if batch.status == "Reviewed":
        approve_activation_batch(batch.name)
    elif batch.status not in {"Approved", "Applied"}:
        frappe.throw(f"Automatic batch is not applicable from status {batch.status}")
    if batch.status == "Applied":
        _update_last_run(status="Already Applied", batch=batch.name)
        return {"status": "Already Applied", "batch": batch.name}

    # Control actions and direct master-switch saves must wait on this lock.
    # Conversely, a control change committed before this point changes the
    # revision and makes this frozen batch fail closed before any identity write.
    _lock_settings()
    locked_settings = frappe.get_single(SETTINGS_DOCTYPE)
    locked_blockers = _configuration_blockers(locked_settings, require_enabled=True)
    if int(locked_settings.automation_control_revision or 0) != frozen_revision:
        locked_blockers.append("automation_control_revision_changed")
    if locked_blockers:
        frappe.db.rollback()
        _update_last_run(
            status="Blocked Before Apply",
            batch=batch.name,
            error=", ".join(sorted(set(locked_blockers))),
        )
        return {
            "status": "Blocked Before Apply",
            "batch": batch.name,
            "blockers": sorted(set(locked_blockers)),
        }
    result = _apply_activation_batch(batch.name, allow_automatic=True)
    _update_last_run(status=str(result["status"]), batch=batch.name)
    return {
        **result,
        "automatic": True,
        "control_revision": frozen_revision,
        "skipped_unsafe_component_count": created.get(
            "skipped_unsafe_component_count", 0
        ),
    }


def run_automatic_tiered_cycle(*, scheduled: bool = False) -> dict[str, Any]:
    """Run one bounded transaction; scheduler calls this only after QC monitoring."""
    previous_user = frappe.session.user
    switched_user = False
    if scheduled and previous_user != "Administrator":
        frappe.set_user("Administrator")
        switched_user = True
    try:
        return _execute_cycle()
    except Exception as exc:
        frappe.db.rollback()
        _update_last_run(
            status="Failed",
            error=f"{type(exc).__name__}:{str(exc)[:110]}",
        )
        frappe.log_error(
            title="Automatic Tiered materialization failed",
            message=frappe.get_traceback(),
        )
        if not scheduled:
            raise
        return {
            "status": "Failed",
            "batch": "",
            "error": f"{type(exc).__name__}:{str(exc)[:110]}",
        }
    finally:
        if switched_user:
            frappe.set_user(previous_user)


@frappe.whitelist()
def run_automatic_tiered_now() -> dict[str, Any]:
    _require_manager()
    return run_automatic_tiered_cycle(scheduled=False)
