"""Component-atomic Tiered Evidence activation batches and deliberate holds."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

import frappe

from db_connector.api_fuzzy_canary import (
    _change_recommendation_status,
    _pair_evidence_payload,
    _refresh_run_counts,
    _snapshot_hash,
)
from db_connector.api_fuzzy_evaluation import SENSITIVE_ROLE
from db_connector.api_identity_resolution import (
    materialize_identity,
    preview_materialization,
)
from db_connector.fuzzy_matching.identity import expected_identity_fingerprints
from db_connector.fuzzy_matching.overlap import structural_overlap_only

RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
BATCH_DOCTYPE = "CCD Identity Activation Batch"
EVENT_DOCTYPE = "CCD Match Recommendation Event"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_batch_reader() -> None:
    roles = set(frappe.get_roles())
    if "System Manager" not in roles and SENSITIVE_ROLE not in roles:
        frappe.throw(
            "System Manager or CCD Match Sensitive Reviewer role is required",
            frappe.PermissionError,
        )


def _run(run_name: str) -> Any:
    run = frappe.get_doc(RUN_DOCTYPE, run_name)
    if run.status not in {"Ready", "Active"}:
        frappe.throw("Activation planning requires a Ready or Active canary")
    if _snapshot_hash(run.policy_snapshot_json) != run.policy_snapshot_sha256:
        frappe.throw("The frozen canary policy snapshot is corrupt")
    return run


def _component_rows(run_name: str) -> dict[str, list[Any]]:
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name, "status": "Proposed"},
        fields=[
            "name",
            "cluster_fingerprint",
            "left_record",
            "right_record",
            "left_source",
            "right_source",
            "source_pair",
            "left_modified_at",
            "right_modified_at",
            "left_identity_fingerprint",
            "right_identity_fingerprint",
            "rollout_state",
            "hold_reason",
            "reason_codes_json",
            "safety_reasons_json",
        ],
        order_by="cluster_fingerprint, name",
        limit_page_length=100_000,
    )
    output: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        output[str(row.cluster_fingerprint)].append(row)
    return dict(sorted(output.items()))


def _source_pair_labels(rows: Iterable[Any]) -> list[str]:
    labels: set[str] = set()
    for row in rows:
        label = str(row.get("source_pair") or "").strip()
        if not label:
            sources = sorted(
                {
                    str(row.get("left_source") or "").strip(),
                    str(row.get("right_source") or "").strip(),
                }
                - {""}
            )
            label = " ↔ ".join(sources)
        if label:
            labels.add(label)
    return sorted(labels)


def backfill_activation_item_source_pairs() -> dict[str, int]:
    """Populate the non-sensitive source summary for batches created earlier."""
    item_doctype = "CCD Identity Activation Item"
    if not frappe.db.table_exists(item_doctype) or not frappe.db.table_exists(
        RECOMMENDATION_DOCTYPE
    ):
        return {"updated": 0, "skipped": 0}
    updated = skipped = 0
    for item in frappe.get_all(
        item_doctype,
        fields=["name", "source_pairs", "recommendation_names_json"],
        limit_page_length=100_000,
    ):
        if str(item.source_pairs or "").strip():
            continue
        try:
            recommendation_names = json.loads(item.recommendation_names_json or "[]")
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not recommendation_names:
            skipped += 1
            continue
        rows = frappe.get_all(
            RECOMMENDATION_DOCTYPE,
            filters={"name": ["in", recommendation_names]},
            fields=["left_source", "right_source", "source_pair"],
            limit_page_length=100_000,
        )
        labels = _source_pair_labels(rows)
        if not labels:
            skipped += 1
            continue
        frappe.db.set_value(
            item_doctype,
            item.name,
            "source_pairs",
            ", ".join(labels),
            update_modified=False,
        )
        updated += 1
    return {"updated": updated, "skipped": skipped}


def _component_context(rows: list[Any]) -> dict[str, Any]:
    record_ids = sorted(
        {
            str(item)
            for row in rows
            for item in (row.left_record, row.right_record)
        }
    )
    expected_modified: dict[str, str] = {}
    fingerprint_values = []
    for row in rows:
        for record_id, modified in (
            (row.left_record, row.left_modified_at),
            (row.right_record, row.right_modified_at),
        ):
            key = str(record_id)
            value = str(modified or "")
            prior = expected_modified.setdefault(key, value)
            if prior != value:
                frappe.throw("A component has inconsistent frozen modified timestamps")
        fingerprint_values.extend(
            (
                (str(row.left_record), row.left_identity_fingerprint),
                (str(row.right_record), row.right_identity_fingerprint),
            )
        )
    try:
        expected_fingerprints = expected_identity_fingerprints(fingerprint_values)
    except ValueError as exc:
        frappe.throw(str(exc))
    return {
        "record_ids": record_ids,
        "expected_modified": expected_modified,
        "expected_fingerprints": expected_fingerprints,
        "recommendations": [str(row.name) for row in rows],
    }


def _held(rows: list[Any]) -> bool:
    states = {str(row.rollout_state or "Available") for row in rows}
    if "Held" in states and len(states) != 1:
        frappe.throw("A component has inconsistent hold state")
    return states == {"Held"}


def _selected_components(
    run_name: str,
    *,
    component_keys: Iterable[str] | None = None,
    component_limit: int | None = None,
) -> list[tuple[str, list[Any]]]:
    components = _component_rows(run_name)
    if component_keys is None:
        selected = [item for item in components.items() if not _held(item[1])]
    else:
        requested = tuple(sorted({str(item) for item in component_keys}))
        missing = [key for key in requested if key not in components]
        if missing:
            frappe.throw("Unknown or non-Proposed component selection")
        selected = [(key, components[key]) for key in requested]
        if any(_held(rows) for _key, rows in selected):
            frappe.throw("Release held components before selecting them")
    if component_limit is not None:
        limit = int(component_limit)
        if limit <= 0:
            frappe.throw("Component limit must be greater than zero")
        selected = selected[:limit]
    return selected


def _preview_components(run: Any, selected: list[tuple[str, list[Any]]]) -> dict[str, Any]:
    conflict_counts: dict[str, int] = defaultdict(int)
    safe = stale = planned_memberships = 0
    component_summaries = []
    for component_key, rows in selected:
        context = _component_context(rows)
        preview = preview_materialization(
            origin="Tiered Evidence",
            origin_doctype=BATCH_DOCTYPE,
            origin_document=f"preview:{run.name}:{component_key}",
            policy_snapshot_json=run.policy_snapshot_json,
            record_ids=context["record_ids"],
            groups=[context["record_ids"]],
            expected_fingerprints=context["expected_fingerprints"] or None,
            expected_modified=context["expected_modified"],
        )
        conflicts = set(preview["conflicts"])
        for reason in conflicts:
            conflict_counts[reason] += 1
        if conflicts:
            stale += int(
                "source_modified_after_canary_snapshot" in conflicts
                or "source_modified_after_snapshot" in conflicts
                or "identity_fingerprint_changed" in conflicts
            )
        else:
            safe += 1
            planned_memberships += int(preview["membership_count"])
        component_summaries.append(
            {
                "component_fingerprint": component_key,
                "recommendation_names": [str(row.name) for row in rows],
                "recommendation_count": len(rows),
                "record_count": len(context["record_ids"]),
                "safe": not conflicts,
                "conflicts": sorted(conflicts),
            }
        )
    return {
        "run": run.name,
        "zero_write": True,
        "selected_component_count": len(selected),
        "selected_recommendation_count": sum(len(rows) for _key, rows in selected),
        "safe_component_count": safe,
        "unsafe_component_count": len(selected) - safe,
        "stale_component_count": stale,
        "planned_identity_group_count": safe,
        "planned_membership_count": planned_memberships,
        "conflict_counts": dict(sorted(conflict_counts.items())),
        "components": component_summaries,
    }


@frappe.whitelist()
def preview_approve_all(run_name: str) -> dict[str, Any]:
    """Evaluate the exact current all-eligible selector with zero writes."""
    _require_manager()
    run = _run(run_name)
    selected = _selected_components(run.name)
    return _preview_components(run, selected)


def _selection_fingerprint(
    run_name: str,
    selection_method: str,
    selected: list[tuple[str, list[Any]]],
    automation_control_revision: int = 0,
) -> str:
    payload = {
        "run": run_name,
        "selection_method": selection_method,
        "components": [
            {
                "component": key,
                "recommendations": [str(row.name) for row in rows],
            }
            for key, rows in selected
        ],
    }
    if automation_control_revision:
        payload["automation_control_revision"] = int(automation_control_revision)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _create_activation_batch(
    run_name: str,
    selection_method: str = "Explicit Wave",
    component_limit: int | str | None = None,
    component_keys_json: str | list[str] | None = None,
    is_pilot_wave: int | str = 0,
    is_demonstration: int | str = 0,
    *,
    allow_structural_overlap: bool = False,
    is_automatic: bool = False,
    automation_control_revision: int = 0,
    automation_authorization_event: str = "",
) -> dict[str, Any]:
    _require_manager()
    allowed_methods = {
        "Explicit Wave",
        "Approve All Eligible",
        "Approve All Remaining",
        "Synthetic Test",
    }
    if allow_structural_overlap:
        allowed_methods.add("Overlap Resolution")
    if is_automatic:
        allowed_methods.add("Automatic Tiered")
    if selection_method not in allowed_methods:
        frappe.throw("Unsupported Activation Batch selection method")
    run = _run(run_name)
    if isinstance(component_keys_json, str):
        component_keys = json.loads(component_keys_json or "[]")
    else:
        component_keys = component_keys_json
    limit = int(component_limit) if component_limit not in (None, "") else None
    selected = _selected_components(
        run.name,
        component_keys=component_keys,
        component_limit=limit,
    )
    if not selected:
        frappe.throw("No available Proposed components were selected")
    preview = _preview_components(run, selected)
    if preview["unsafe_component_count"]:
        if not allow_structural_overlap:
            frappe.throw("Activation Batch selection contains stale or unsafe components")
        unsafe = [row for row in preview["components"] if not row["safe"]]
        if (
            len(selected) != 1
            or len(unsafe) != 1
            or not structural_overlap_only(
                unsafe[0]["conflicts"], stale=bool(preview["stale_component_count"])
            )
        ):
            frappe.throw(
                "Only one current component with structural identity overlap may use an Overlap Resolution Batch"
            )
    elif allow_structural_overlap:
        frappe.throw("This component is safe; use a normal Activation Batch")
    selection_fingerprint = _selection_fingerprint(
        run.name,
        selection_method,
        selected,
        int(automation_control_revision or 0) if is_automatic else 0,
    )
    existing = frappe.db.get_value(
        BATCH_DOCTYPE, {"selection_fingerprint": selection_fingerprint}, "name"
    )
    if existing:
        return {"batch": existing, "status": frappe.db.get_value(BATCH_DOCTYPE, existing, "status")}
    idempotency_key = hashlib.sha256(
        f"activation-batch-v1\x1f{selection_fingerprint}".encode()
    ).hexdigest()
    now = frappe.utils.now_datetime()
    batch = frappe.get_doc(
        {
            "doctype": BATCH_DOCTYPE,
            "canary_run": run.name,
            "matching_policy": run.matching_policy,
            "policy_version": run.policy_version,
            "policy_snapshot_sha256": run.policy_snapshot_sha256,
            "snapshot_at": run.snapshot_at,
            "selection_method": selection_method,
            "selection_fingerprint": selection_fingerprint,
            "idempotency_key": idempotency_key,
            "is_pilot_wave": int(is_pilot_wave or 0),
            "is_demonstration": int(is_demonstration or 0),
            "is_automatic": int(is_automatic),
            "automation_control_revision": int(automation_control_revision or 0),
            "automation_authorization_event": automation_authorization_event or None,
            "status": "Reviewed",
            "selected_component_count": preview["selected_component_count"],
            "selected_recommendation_count": preview["selected_recommendation_count"],
            "planned_group_count": preview["planned_identity_group_count"],
            "planned_membership_count": preview["planned_membership_count"],
            "stale_count": preview["stale_component_count"],
            "new_exception_count": preview["unsafe_component_count"],
            "dry_run_at": now,
            "dry_run_by": frappe.session.user,
            "dry_run_json": _json({key: value for key, value in preview.items() if key != "components"}),
        }
    )
    for (component_key, rows), summary in zip(selected, preview["components"], strict=True):
        context = _component_context(rows)
        batch.append(
            "items",
            {
                "component_fingerprint": component_key,
                "recommendation_count": len(rows),
                "record_count": len(context["record_ids"]),
                "source_pairs": ", ".join(_source_pair_labels(rows)),
                "status": "Exception" if summary["conflicts"] else "Planned",
                "recommendation_names_json": _json(context["recommendations"]),
                "planned_group_key": hashlib.sha256(
                    f"{idempotency_key}\x1f{component_key}".encode()
                ).hexdigest(),
                "error_code": (
                    "overlap_resolution_required:" + ",".join(summary["conflicts"])
                    if summary["conflicts"]
                    else ""
                ),
            },
        )
    batch.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"batch": batch.name, "status": batch.status, **{k: v for k, v in preview.items() if k != "components"}}


def preview_automatic_component_selection(
    run_name: str, component_limit: int
) -> dict[str, Any]:
    """Select the first bounded safe components while reporting skipped conflicts."""
    run = _run(run_name)
    limit = int(component_limit or 0)
    if limit < 1 or limit > 100:
        frappe.throw("Automatic component limit must be between 1 and 100")
    selected: list[tuple[str, list[Any]]] = []
    skipped = []
    for component_key, rows in _selected_components(run.name):
        summary = _preview_components(run, [(component_key, rows)])["components"][0]
        if summary["safe"]:
            selected.append((component_key, rows))
            if len(selected) >= limit:
                break
        else:
            skipped.append(summary)
    preview = _preview_components(run, selected) if selected else {
        "run": run.name,
        "zero_write": True,
        "selected_component_count": 0,
        "selected_recommendation_count": 0,
        "safe_component_count": 0,
        "unsafe_component_count": 0,
        "stale_component_count": 0,
        "planned_identity_group_count": 0,
        "planned_membership_count": 0,
        "conflict_counts": {},
        "components": [],
    }
    preview["component_keys"] = [key for key, _rows in selected]
    preview["skipped_unsafe_component_count"] = len(skipped)
    preview["skipped_components"] = skipped
    return preview


def create_automatic_activation_batch(
    run_name: str,
    component_limit: int,
    automation_control_revision: int,
    automation_authorization_event: str,
) -> dict[str, Any]:
    """Freeze one pre-authorized bounded automatic batch; applying is separate."""
    _require_manager()
    selection = preview_automatic_component_selection(run_name, component_limit)
    if not selection["component_keys"]:
        return {
            "status": "No Eligible Components",
            "batch": "",
            **selection,
        }
    result = _create_activation_batch(
        run_name,
        "Automatic Tiered",
        None,
        selection["component_keys"],
        0,
        0,
        allow_structural_overlap=False,
        is_automatic=True,
        automation_control_revision=int(automation_control_revision or 0),
        automation_authorization_event=automation_authorization_event,
    )
    return {
        **result,
        "skipped_unsafe_component_count": selection[
            "skipped_unsafe_component_count"
        ],
        "skipped_components": selection["skipped_components"],
    }


@frappe.whitelist()
def create_activation_batch(
    run_name: str,
    selection_method: str = "Explicit Wave",
    component_limit: int | str | None = None,
    component_keys_json: str | list[str] | None = None,
    is_pilot_wave: int | str = 0,
    is_demonstration: int | str = 0,
) -> dict[str, Any]:
    return _create_activation_batch(
        run_name,
        selection_method,
        component_limit,
        component_keys_json,
        is_pilot_wave,
        is_demonstration,
        allow_structural_overlap=False,
    )


@frappe.whitelist()
def create_overlap_resolution_batch(
    recommendation_name: str,
    is_demonstration: int | str = 0,
) -> dict[str, Any]:
    """Freeze one structurally overlapping Tiered component for approval."""
    _require_manager()
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if recommendation.status != "Proposed" or recommendation.rollout_state == "Held":
        frappe.throw("Select an available Proposed Tiered recommendation")
    return _create_activation_batch(
        str(recommendation.canary_run),
        "Overlap Resolution",
        None,
        [str(recommendation.cluster_fingerprint)],
        0,
        is_demonstration,
        allow_structural_overlap=True,
    )


@frappe.whitelist()
def get_activation_batch_component(batch_name: str, item_name: str) -> dict[str, Any]:
    """Return the frozen component with role-protected records and pair evidence."""
    _require_batch_reader()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    item = next((row for row in batch.items if str(row.name) == str(item_name)), None)
    if not item:
        frappe.throw("The selected component is not an item in this Activation Batch")

    try:
        recommendation_names = json.loads(item.recommendation_names_json or "[]")
    except (TypeError, ValueError):
        frappe.throw("The frozen recommendation selection is corrupt")
    if (
        not isinstance(recommendation_names, list)
        or not recommendation_names
        or any(not isinstance(name, str) or not name for name in recommendation_names)
    ):
        frappe.throw("The frozen recommendation selection is corrupt")

    recommendations = [
        frappe.get_doc(RECOMMENDATION_DOCTYPE, name)
        for name in recommendation_names
    ]
    component_fingerprint = str(item.component_fingerprint)
    if any(
        str(row.canary_run) != str(batch.canary_run)
        or str(row.cluster_fingerprint) != component_fingerprint
        for row in recommendations
    ):
        frappe.throw("A frozen recommendation no longer belongs to this component")

    record_sources: dict[str, str] = {}
    for row in recommendations:
        for record_id, source in (
            (str(row.left_record), str(row.left_source or "")),
            (str(row.right_record), str(row.right_source or "")),
        ):
            existing = record_sources.get(record_id)
            if existing is not None and existing != source:
                frappe.throw("A frozen component has inconsistent record sources")
            record_sources[record_id] = source
    aliases = {
        record_id: f"R{index}"
        for index, record_id in enumerate(sorted(record_sources), start=1)
    }
    if len(recommendations) != int(item.recommendation_count or 0):
        frappe.throw("The frozen recommendation count does not match the batch item")
    if len(record_sources) != int(item.record_count or 0):
        frappe.throw("The frozen record count does not match the batch item")

    pair_payloads: list[dict[str, Any]] = []
    sensitive_values_visible = False
    for recommendation in recommendations:
        payload = _pair_evidence_payload(recommendation)
        sensitive_values_visible = bool(payload["sensitive_values_visible"])
        payload["left"]["alias"] = aliases[str(recommendation.left_record)]
        payload["right"]["alias"] = aliases[str(recommendation.right_record)]
        pair_payloads.append(payload)

    records = []
    for record_id in sorted(record_sources):
        record = {
            "alias": aliases[record_id],
            "source": record_sources[record_id],
        }
        if sensitive_values_visible:
            record["record_id"] = record_id
        records.append(record)

    return {
        "batch": batch.name,
        "batch_status": batch.status,
        "item": item.name,
        "item_status": item.status,
        "component_fingerprint": component_fingerprint,
        "source_pairs": _source_pair_labels(recommendations),
        "recommendation_count": len(recommendations),
        "record_count": len(records),
        "records": records,
        "recommendations": pair_payloads,
        "sensitive_values_visible": sensitive_values_visible,
        "is_demonstration": bool(batch.is_demonstration),
    }


@frappe.whitelist()
def approve_activation_batch(batch_name: str) -> dict[str, str]:
    _require_manager()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.status == "Approved":
        return {"batch": batch.name, "status": batch.status}
    if batch.status != "Reviewed":
        frappe.throw("Only a reviewed Activation Batch may be approved")
    batch.db_set(
        {
            "status": "Approved",
            "approved_at": frappe.utils.now_datetime(),
            "approved_by": frappe.session.user,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {"batch": batch.name, "status": "Approved"}


@frappe.whitelist()
def revalidate_failed_activation_batch(batch_name: str) -> dict[str, Any]:
    """Re-run the frozen selection preview before allowing a failed retry."""
    _require_manager()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.is_automatic:
        frappe.throw(
            "An automatic batch is immutable and cannot be manually revalidated; "
            "a later authorized cycle must create a new frozen batch"
        )
    if batch.status != "Failed":
        frappe.throw("Only a failed Activation Batch can be revalidated for retry")
    run = _run(batch.canary_run)
    components = _component_rows(run.name)
    selected: list[tuple[str, list[Any]]] = []
    for item in batch.items:
        if item.status in {"Applied", "Already Applied", "Corrected"}:
            continue
        component_key = str(item.component_fingerprint)
        rows = components.get(component_key)
        expected_names = sorted(json.loads(item.recommendation_names_json or "[]"))
        if (
            not rows
            or sorted(str(row.name) for row in rows) != expected_names
            or _held(rows)
        ):
            frappe.throw("The failed batch selection is no longer fully available")
        selected.append((component_key, rows))
    preview = _preview_components(run, selected)
    if preview["unsafe_component_count"]:
        frappe.throw("The failed batch remains stale or unsafe and cannot be retried")
    batch.db_set(
        {"status": "Approved", "error_summary": ""},
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "batch": batch.name,
        "status": "Approved",
        **{key: value for key, value in preview.items() if key != "components"},
    }


def _reasons(rows: list[Any]) -> list[str]:
    values: set[str] = set()
    for row in rows:
        for fieldname in ("reason_codes_json", "safety_reasons_json"):
            values.update(str(item) for item in json.loads(row.get(fieldname) or "[]"))
    return sorted(values)


def _automatic_batch_authorization_blockers(batch: Any) -> list[str]:
    """Recheck every unattended-write control while holding the Settings lock."""
    from db_connector.api_identity_automation import _configuration_blockers
    from db_connector.api_identity_qc import _lock_settings

    _lock_settings()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    blockers = _configuration_blockers(settings, require_enabled=True)
    if int(settings.automation_control_revision or 0) != int(
        batch.automation_control_revision or 0
    ):
        blockers.append("automation_control_revision_changed")
    if str(settings.automatic_tiered_authorization_event or "") != str(
        batch.automation_authorization_event or ""
    ):
        blockers.append("automation_authorization_event_changed")
    if str(settings.automatic_tiered_canary or "") != str(batch.canary_run or ""):
        blockers.append("authorized_canary_changed")
    if str(settings.automatic_tiered_policy or "") != str(batch.matching_policy or ""):
        blockers.append("authorized_policy_changed")
    return sorted(set(blockers))


def _apply_activation_batch(
    batch_name: str, *, allow_automatic: bool = False
) -> dict[str, Any]:
    _require_manager()
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.is_automatic:
        if not allow_automatic:
            frappe.throw(
                "Automatic Tiered batches can be applied only by the governed automation worker"
            )
        automatic_blockers = _automatic_batch_authorization_blockers(batch)
        if automatic_blockers:
            frappe.throw(
                "Automatic Tiered batch authorization is no longer valid: "
                + ", ".join(automatic_blockers)
            )
        # Refetch after the Settings lock so this transaction cannot continue
        # with a stale batch object while a competing cycle advances it.
        batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    if batch.status == "Applied":
        return {
            "batch": batch.name,
            "status": "Applied",
            "created_groups": batch.created_group_count,
            "created_memberships": batch.created_membership_count,
        }
    if batch.status != "Approved":
        frappe.throw("Only an approved Activation Batch may be applied")
    run = _run(batch.canary_run)
    if run.policy_snapshot_sha256 != batch.policy_snapshot_sha256:
        frappe.throw("Activation Batch and canary policy snapshots differ")
    if any(item.status == "Exception" for item in batch.items):
        frappe.throw(
            "This batch contains a structural overlap; use Resolve Overlap on its Exception item"
        )
    batch.db_set("status", "Applying", update_modified=False)
    try:
        created_groups = int(batch.created_group_count or 0)
        created_memberships = int(batch.created_membership_count or 0)
        components = _component_rows(run.name)
        for item in batch.items:
            if item.status in {"Applied", "Already Applied", "Corrected"}:
                continue
            rows = components.get(str(item.component_fingerprint))
            if not rows:
                frappe.throw("A selected component is no longer fully Proposed")
            expected_names = sorted(json.loads(item.recommendation_names_json or "[]"))
            if sorted(str(row.name) for row in rows) != expected_names:
                frappe.throw("A selected component changed after batch review")
            if _held(rows):
                frappe.throw("A selected component was held after batch review")
            context = _component_context(rows)
            result = materialize_identity(
                origin="Tiered Evidence",
                origin_doctype=BATCH_DOCTYPE,
                origin_document=batch.name,
                policy_snapshot_json=run.policy_snapshot_json,
                policy_snapshot_sha256=run.policy_snapshot_sha256,
                matching_policy=run.matching_policy,
                record_ids=context["record_ids"],
                groups=[context["record_ids"]],
                expected_fingerprints=context["expected_fingerprints"] or None,
                expected_modified=context["expected_modified"],
                reason_codes=_reasons(rows),
                review_context={
                    "activation_batch": batch.name,
                    "component_fingerprint": item.component_fingerprint,
                    "selection_method": batch.selection_method,
                },
                is_demonstration=bool(batch.is_demonstration),
            )
            created_groups += int(result["created_groups"])
            created_memberships += int(result["created_memberships"])
            group_name = (result.get("identity_groups") or [""])[0]
            for row in rows:
                recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, row.name)
                _change_recommendation_status(
                    recommendation,
                    "Approved",
                    "Approved",
                    f"materialized_by_activation_batch:{batch.name}",
                    approved=True,
                )
                frappe.db.set_value(
                    RECOMMENDATION_DOCTYPE,
                    recommendation.name,
                    {
                        "rollout_state": "Applied",
                        "activation_batch": batch.name,
                        "identity_decision": result["identity_decision"],
                        "identity_group": group_name or None,
                    },
                    update_modified=False,
                )
            item.db_set(
                {
                    "status": "Applied" if result["status"] == "Applied" else "Already Applied",
                    "identity_decision": result["identity_decision"],
                    "identity_group": group_name or None,
                },
                update_modified=False,
            )
        counts = _refresh_run_counts(run.name)
        now = frappe.utils.now_datetime()
        frappe.db.set_value(
            RUN_DOCTYPE,
            run.name,
            {
                "status": "Active",
                "approved_at": now,
                "approved_by": frappe.session.user,
                "materialized_group_count": frappe.db.count(
                    "CCD Identity Group", {"status": ["in", ["Active", "Needs Revalidation"]]}
                ),
                "materialized_membership_count": frappe.db.count(
                    "CCD Identity Membership", {"status": ["in", ["Active", "Needs Revalidation"]]}
                ),
            },
            update_modified=False,
        )
        frappe.db.set_value(
            BATCH_DOCTYPE,
            batch.name,
            {
                "status": "Applied",
                "created_group_count": created_groups,
                "created_membership_count": created_memberships,
                "applied_at": now,
                "applied_by": frappe.session.user,
                "error_summary": "",
            },
            update_modified=False,
        )
        frappe.db.commit()
        return {
            "batch": batch.name,
            "status": "Applied",
            "created_groups": created_groups,
            "created_memberships": created_memberships,
            "approved_recommendations": counts["active_count"],
        }
    except Exception as exc:
        frappe.db.rollback()
        frappe.db.set_value(
            BATCH_DOCTYPE,
            batch.name,
            {"status": "Failed", "error_summary": f"{type(exc).__name__}:{str(exc)[:120]}"},
            update_modified=False,
        )
        frappe.db.commit()
        raise


@frappe.whitelist()
def apply_activation_batch(batch_name: str) -> dict[str, Any]:
    """Apply a manager-controlled manual batch; automatic batches are private."""
    return _apply_activation_batch(batch_name, allow_automatic=False)


def _set_component_hold(recommendation_name: str, *, held: bool, reason: str = "") -> dict[str, Any]:
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if recommendation.status != "Proposed":
        frappe.throw("Only Proposed recommendations can be held or released")
    if held and not str(reason or "").strip():
        frappe.throw("A hold reason is required")
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={
            "canary_run": recommendation.canary_run,
            "cluster_fingerprint": recommendation.cluster_fingerprint,
            "status": "Proposed",
        },
        fields=["name"],
        limit_page_length=100_000,
    )
    if not rows:
        frappe.throw("The complete Proposed component is unavailable")
    now = frappe.utils.now_datetime()
    values = (
        {
            "rollout_state": "Held",
            "hold_reason": str(reason).strip(),
            "held_at": now,
            "held_by": frappe.session.user,
        }
        if held
        else {
            "rollout_state": "Available",
            "hold_reason": "",
            "held_at": None,
            "held_by": None,
        }
    )
    for row in rows:
        frappe.db.set_value(RECOMMENDATION_DOCTYPE, row.name, values, update_modified=False)
        frappe.get_doc(
            {
                "doctype": EVENT_DOCTYPE,
                "recommendation": row.name,
                "canary_run": recommendation.canary_run,
                "event_type": "Held" if held else "Released",
                "from_status": "Proposed",
                "to_status": "Proposed",
                "reason": str(reason).strip() if held else "deliberate_hold_released",
                "event_at": now,
                "actor": frappe.session.user,
                "metadata_json": _json(
                    {"cluster_fingerprint": recommendation.cluster_fingerprint}
                ),
            }
        ).insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "component_fingerprint": recommendation.cluster_fingerprint,
        "recommendation_count": len(rows),
        "rollout_state": "Held" if held else "Available",
    }


@frappe.whitelist()
def hold_component(recommendation_name: str, reason: str) -> dict[str, Any]:
    _require_manager()
    return _set_component_hold(recommendation_name, held=True, reason=reason)


@frappe.whitelist()
def release_component_hold(recommendation_name: str) -> dict[str, Any]:
    _require_manager()
    return _set_component_hold(recommendation_name, held=False)
