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

RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
BATCH_DOCTYPE = "CCD Identity Activation Batch"
EVENT_DOCTYPE = "CCD Match Recommendation Event"


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


def _record_modified_values(record_ids: Iterable[str]) -> dict[str, str]:
    ids = tuple(sorted({str(item) for item in record_ids}))
    output: dict[str, str] = {}
    for index in range(0, len(ids), 500):
        for row in frappe.get_all(
            "CCD Master",
            filters={"name": ["in", ids[index : index + 500]]},
            fields=["name", "modified"],
            limit_page_length=500,
        ):
            output[str(row.name)] = str(row.modified or "")
    return output


def _component_context(rows: list[Any]) -> dict[str, Any]:
    record_ids = sorted(
        {
            str(item)
            for row in rows
            for item in (row.left_record, row.right_record)
        }
    )
    expected_modified: dict[str, str] = {}
    expected_fingerprints: dict[str, str] = {}
    for row in rows:
        expected_modified[str(row.left_record)] = str(row.left_modified_at or "")
        expected_modified[str(row.right_record)] = str(row.right_modified_at or "")
        if row.left_identity_fingerprint:
            expected_fingerprints[str(row.left_record)] = str(row.left_identity_fingerprint)
        if row.right_identity_fingerprint:
            expected_fingerprints[str(row.right_record)] = str(row.right_identity_fingerprint)
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
    all_record_ids = {
        str(item)
        for _key, rows in selected
        for row in rows
        for item in (row.left_record, row.right_record)
    }
    current_modified = _record_modified_values(all_record_ids)
    conflict_counts: dict[str, int] = defaultdict(int)
    safe = stale = planned_memberships = 0
    component_summaries = []
    for component_key, rows in selected:
        context = _component_context(rows)
        modified_stale = any(
            current_modified.get(record_id, "") != expected
            for record_id, expected in context["expected_modified"].items()
        )
        preview = preview_materialization(
            origin="Tiered Evidence",
            origin_doctype=BATCH_DOCTYPE,
            origin_document=f"preview:{run.name}:{component_key}",
            policy_snapshot_json=run.policy_snapshot_json,
            record_ids=context["record_ids"],
            groups=[context["record_ids"]],
            expected_fingerprints=context["expected_fingerprints"] or None,
        )
        conflicts = set(preview["conflicts"])
        if modified_stale:
            conflicts.add("source_modified_after_canary_snapshot")
        for reason in conflicts:
            conflict_counts[reason] += 1
        if conflicts:
            stale += int(
                "source_modified_after_canary_snapshot" in conflicts
                or "identity_fingerprint_changed" in conflicts
            )
        else:
            safe += 1
            planned_memberships += int(preview["membership_count"])
        component_summaries.append(
            {
                "component_fingerprint": component_key,
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
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@frappe.whitelist()
def create_activation_batch(
    run_name: str,
    selection_method: str = "Explicit Wave",
    component_limit: int | str | None = None,
    component_keys_json: str | list[str] | None = None,
    is_pilot_wave: int | str = 0,
    is_demonstration: int | str = 0,
) -> dict[str, Any]:
    _require_manager()
    allowed_methods = {
        "Explicit Wave",
        "Approve All Eligible",
        "Approve All Remaining",
        "Synthetic Test",
    }
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
        frappe.throw("Activation Batch selection contains stale or unsafe components")
    selection_fingerprint = _selection_fingerprint(run.name, selection_method, selected)
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
                "status": "Planned",
                "recommendation_names_json": _json(context["recommendations"]),
                "planned_group_key": hashlib.sha256(
                    f"{idempotency_key}\x1f{component_key}".encode()
                ).hexdigest(),
            },
        )
    batch.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"batch": batch.name, "status": batch.status, **{k: v for k, v in preview.items() if k != "components"}}


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
    if batch.status != "Failed":
        frappe.throw("Only a failed Activation Batch can be revalidated for retry")
    run = _run(batch.canary_run)
    components = _component_rows(run.name)
    selected: list[tuple[str, list[Any]]] = []
    for item in batch.items:
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


@frappe.whitelist()
def apply_activation_batch(batch_name: str) -> dict[str, Any]:
    _require_manager()
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
    batch.db_set("status", "Applying", update_modified=False)
    try:
        created_groups = created_memberships = 0
        components = _component_rows(run.name)
        for item in batch.items:
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
