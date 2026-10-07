"""Component-atomic Tiered Evidence activation batches and deliberate holds."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable

import frappe

from db_connector.api_fuzzy_canary import (
    _change_recommendation_status,
    _materialized_component_matches,
    _pair_evidence_payload,
    _refresh_run_counts,
    _snapshot_hash,
    reconcile_materialized_recommendations,
)
from db_connector.api_fuzzy_evaluation import SENSITIVE_ROLE
from db_connector.api_identity_resolution import (
    CURRENT_MEMBERSHIP_STATUSES,
    EXCLUSION_DOCTYPE,
    MEMBERSHIP_DOCTYPE,
    _record_rows,
    materialize_identity,
)
from db_connector.fuzzy_matching.identity import (
    complete_hkid_conflicts,
    expected_identity_fingerprints,
    fingerprint_scoped_exclusion_conflicts,
    identity_fingerprint,
    snapshot_modified_conflicts,
)
from db_connector.fuzzy_matching.overlap import structural_overlap_only
from db_connector.fuzzy_matching.policy import MatchingPolicy

RUN_DOCTYPE = "CCD Match Canary Run"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
BATCH_DOCTYPE = "CCD Identity Activation Batch"
EVENT_DOCTYPE = "CCD Match Recommendation Event"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
CREATION_OPERATION_TTL_SECONDS = 21_600
CREATION_OPERATION_TIMEOUT_SECONDS = 7_200
AUTOMATIC_COMPONENT_PAGE_SIZE = 200
RUN_COUNT_RECONCILIATION_TIMEOUT_SECONDS = 1_800
PREVIEW_COMPONENT_PAGE_SIZE = 250
PREVIEW_OPERATION_TTL_SECONDS = 21_600
PREVIEW_OPERATION_TIMEOUT_SECONDS = 7_200
PREVIEW_UNSAFE_DETAIL_LIMIT = 100
APPLY_OPERATION_TTL_SECONDS = 86_400
APPLY_OPERATION_TIMEOUT_SECONDS = 21_600
APPLY_PROGRESS_INTERVAL = 10


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


def _component_rows(
    run_name: str, component_keys: Iterable[str] | None = None
) -> dict[str, list[Any]]:
    filters: dict[str, Any] = {
        "canary_run": run_name,
        "status": "Proposed",
    }
    if component_keys is not None:
        requested = tuple(
            sorted({str(item) for item in component_keys if str(item)})
        )
        if not requested:
            return {}
        # The composite canary/status/cluster index turns batch revalidation
        # and Apply into a bounded lookup instead of loading every Proposed
        # recommendation in a large canary.
        filters["cluster_fingerprint"] = ["in", requested]
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters=filters,
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
    requested = (
        tuple(sorted({str(item) for item in component_keys}))
        if component_keys is not None
        else None
    )
    components = _component_rows(run_name, component_keys=requested)
    materialized = (
        _materialized_component_matches(run_name, components)
        if requested is not None
        else _materialized_component_matches(run_name)
    )
    components = {
        key: rows for key, rows in components.items() if key not in materialized
    }
    if component_keys is None:
        selected = [item for item in components.items() if not _held(item[1])]
    else:
        assert requested is not None
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


def _automatic_component_key_page(
    run_name: str, *, after_key: str | None, page_size: int
) -> tuple[str, ...]:
    """Return one indexed, deterministic page of Proposed component keys."""
    limit = max(1, min(int(page_size), 1_000))
    cursor_clause = ""
    values: list[Any] = [run_name]
    if after_key is not None:
        cursor_clause = " AND cluster_fingerprint > %s"
        values.append(str(after_key))
    # LIMIT is an internally bounded integer. Keeping it literal also avoids
    # driver-specific handling of a parameter in the LIMIT position.
    rows = frappe.db.sql(
        f"""
        SELECT cluster_fingerprint
          FROM `tabCCD Match Recommendation`
         WHERE canary_run = %s
           AND status = 'Proposed'
           AND cluster_fingerprint IS NOT NULL
           AND cluster_fingerprint != ''
           {cursor_clause}
         GROUP BY cluster_fingerprint
         ORDER BY cluster_fingerprint
         LIMIT {limit}
        """,
        tuple(values),
        as_dict=True,
    )
    return tuple(str(row.cluster_fingerprint) for row in rows)


def _automatic_component_pages(
    run_name: str, *, page_size: int = AUTOMATIC_COMPONENT_PAGE_SIZE
) -> Iterable[tuple[tuple[str, list[Any]], ...]]:
    """Load only the recommendation rows needed by each candidate-key page."""
    cursor: str | None = None
    while True:
        keys = _automatic_component_key_page(
            run_name, after_key=cursor, page_size=page_size
        )
        if not keys:
            return
        components = _component_rows(run_name, component_keys=keys)
        materialized = _materialized_component_matches(run_name, components)
        yield tuple(
            (key, components[key])
            for key in keys
            if key in components
            and key not in materialized
            and not _held(components[key])
        )
        cursor = keys[-1]
        if len(keys) < page_size:
            return


def _matching_policy(snapshot_json: str | dict[str, Any]) -> MatchingPolicy:
    value = json.loads(snapshot_json) if isinstance(snapshot_json, str) else snapshot_json
    return MatchingPolicy.from_dict(value)


def _preview_component_page(
    run: Any,
    selected: list[tuple[str, list[Any]]],
    *,
    policy: MatchingPolicy,
) -> list[dict[str, Any]]:
    """Evaluate one component page with a bounded number of database reads.

    This deliberately mirrors ``preview_materialization`` safety rules for the
    Tiered Evidence path.  The scalar materializer remains authoritative when
    Apply runs; this is a zero-write planning acceleration only.
    """
    contexts = [
        (component_key, rows, _component_context(rows))
        for component_key, rows in selected
    ]
    record_ids = tuple(
        sorted(
            {
                record_id
                for _component_key, _rows, context in contexts
                for record_id in context["record_ids"]
            }
        )
    )
    records = _record_rows(record_ids)
    for row in records.values():
        row["source"] = str(row.get("ccd_reg_source") or row.get("source") or "")
    fingerprints = {
        record_id: identity_fingerprint(row, policy)
        for record_id, row in records.items()
    }

    memberships = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "ccd_master": ["in", record_ids],
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=["ccd_master", "identity_group"],
        limit_page_length=max(len(record_ids) * 2, 1),
    ) if record_ids else []
    memberships_by_record: dict[str, set[str]] = defaultdict(set)
    current_group_names: set[str] = set()
    for membership in memberships:
        record_id = str(membership.ccd_master)
        group_name = str(membership.identity_group)
        memberships_by_record[record_id].add(group_name)
        current_group_names.add(group_name)

    group_members: dict[str, set[str]] = defaultdict(set)
    if current_group_names:
        for membership in frappe.get_all(
            MEMBERSHIP_DOCTYPE,
            filters={
                "identity_group": ["in", tuple(sorted(current_group_names))],
                "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
            },
            fields=["ccd_master", "identity_group"],
            limit_page_length=100_000,
        ):
            group_members[str(membership.identity_group)].add(
                str(membership.ccd_master)
            )

    exclusion_rows: list[tuple[str, str, str, str]] = []
    if len(record_ids) > 1:
        exclusion_rows = [
            (
                str(row.left_record),
                str(row.right_record),
                str(row.left_fingerprint),
                str(row.right_fingerprint),
            )
            for row in frappe.get_all(
                EXCLUSION_DOCTYPE,
                filters={
                    "left_record": ["in", record_ids],
                    "right_record": ["in", record_ids],
                    "status": "Active",
                },
                fields=[
                    "left_record",
                    "right_record",
                    "left_fingerprint",
                    "right_fingerprint",
                ],
                limit_page_length=100_000,
            )
        ]

    summaries: list[dict[str, Any]] = []
    for component_key, rows, context in contexts:
        desired = set(context["record_ids"])
        component_records = {
            record_id: records[record_id] for record_id in context["record_ids"]
        }
        component_fingerprints = {
            record_id: fingerprints[record_id]
            for record_id in context["record_ids"]
        }
        frozen_fingerprints = context["expected_fingerprints"]
        fingerprint_stale_records = sorted(
            record_id
            for record_id, expected in frozen_fingerprints.items()
            if component_fingerprints.get(record_id) != str(expected)
        )
        current_modified = {
            record_id: str(row.get("modified") or "")
            for record_id, row in component_records.items()
        }
        modified_stale_records = snapshot_modified_conflicts(
            context["expected_modified"], current_modified
        )
        missing_fingerprint_records = desired - set(frozen_fingerprints)
        missing_modified_records = desired - set(context["expected_modified"])

        conflicts: set[str] = set()
        if missing_fingerprint_records or missing_modified_records:
            conflicts.add("frozen_identity_snapshot_incomplete")
        if fingerprint_stale_records:
            conflicts.add("identity_fingerprint_changed")
        if modified_stale_records:
            conflicts.add("source_modified_after_snapshot")
        if complete_hkid_conflicts(
            (tuple(context["record_ids"]),), component_records, policy
        ):
            conflicts.add("complete_hkid_conflict")

        existing_groups = {
            group_name
            for record_id in desired
            for group_name in memberships_by_record.get(record_id, set())
        }
        if len(existing_groups) > 1:
            conflicts.add("conflicting_active_identity_groups")
        elif existing_groups:
            existing_group = next(iter(existing_groups))
            if not group_members.get(existing_group, set()).issubset(desired):
                conflicts.add("partial_existing_identity_group")

        component_exclusions = [
            row
            for row in exclusion_rows
            if row[0] in desired and row[1] in desired
        ]
        if fingerprint_scoped_exclusion_conflicts(
            (tuple(context["record_ids"]),),
            component_fingerprints,
            component_exclusions,
        ):
            conflicts.add("active_human_exclusion")

        source_counts = Counter(
            str(component_records[record_id].get("source") or "")
            for record_id in context["record_ids"]
        )
        if any(count > 1 for source, count in source_counts.items() if source):
            conflicts.add("same_source_duplicates_require_human_decision")

        summaries.append(
            {
                "component_fingerprint": component_key,
                "recommendation_names": [str(row.name) for row in rows],
                "recommendation_count": len(rows),
                "record_count": len(context["record_ids"]),
                "safe": not conflicts,
                "conflicts": sorted(conflicts),
            }
        )
    return summaries


def _preview_components(
    run: Any,
    selected: list[tuple[str, list[Any]]],
    *,
    include_safe_components: bool = True,
    component_detail_limit: int | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    conflict_counts: dict[str, int] = defaultdict(int)
    safe = stale = planned_memberships = 0
    component_summaries: list[dict[str, Any]] = []
    omitted_component_detail_count = 0
    total = len(selected)
    policy = _matching_policy(run.policy_snapshot_json)
    for start in range(0, total, PREVIEW_COMPONENT_PAGE_SIZE):
        page = selected[start : start + PREVIEW_COMPONENT_PAGE_SIZE]
        for summary in _preview_component_page(run, page, policy=policy):
            conflicts = set(summary["conflicts"])
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
                planned_memberships += int(summary["record_count"])
            include_detail = include_safe_components or bool(conflicts)
            if include_detail and (
                component_detail_limit is None
                or len(component_summaries) < component_detail_limit
            ):
                component_summaries.append(summary)
            elif include_detail:
                omitted_component_detail_count += 1
        if progress_callback:
            progress_callback(min(start + len(page), total), total)
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
        "omitted_component_detail_count": omitted_component_detail_count,
    }


@frappe.whitelist()
def preview_approve_all(run_name: str) -> dict[str, Any]:
    """Reject the legacy synchronous path before a web worker can time out."""
    _require_manager()
    _run(run_name)
    frappe.throw(
        "Approve-all preview now runs in the background. Reload this form and use Preview Approve All again."
    )


def _preview_operation_key(operation_token: str) -> str:
    return f"ccd_approve_all_preview:{operation_token}"


def _preview_active_key(run_name: str) -> str:
    return f"ccd_approve_all_preview_active:{str(run_name)}"


def _approve_all_preview_operation(operation_token: str) -> dict[str, Any] | None:
    raw = frappe.cache.get_value(_preview_operation_key(operation_token), expires=True)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _set_approve_all_preview_operation(
    operation_token: str, payload: dict[str, Any]
) -> None:
    frappe.cache.set_value(
        _preview_operation_key(operation_token),
        _json(payload),
        expires_in_sec=PREVIEW_OPERATION_TTL_SECONDS,
    )


@frappe.whitelist(methods=["POST"])
def start_approve_all_preview(run_name: str) -> dict[str, Any]:
    """Return promptly and evaluate the zero-write approve-all preview on long."""
    _require_manager()
    run = _run(str(run_name))
    locked = frappe.db.sql(
        "SELECT name FROM `tabCCD Match Canary Run` WHERE name=%s FOR UPDATE",
        (run.name,),
    )
    if not locked:
        frappe.throw("CCD Match Canary Run no longer exists")
    active_key = _preview_active_key(run.name)
    active_token = frappe.cache.get_value(active_key)
    if isinstance(active_token, bytes):
        active_token = active_token.decode("utf-8", "replace")
    existing = (
        _approve_all_preview_operation(str(active_token)) if active_token else None
    )
    if existing and existing.get("status") in {"Queued", "Running"}:
        if existing.get("requested_by") != frappe.session.user:
            frappe.throw("Another System Manager is previewing this canary")
        return {
            "operation_token": str(active_token),
            "status": existing["status"],
            "already_running": True,
        }

    operation_token = uuid.uuid4().hex
    requested_by = str(frappe.session.user)
    payload = {
        "operation_token": operation_token,
        "run_name": run.name,
        "requested_by": requested_by,
        "status": "Queued",
        "stage": "queued",
        "processed_components": 0,
        "total_components": 0,
        "queued_at": str(frappe.utils.now_datetime()),
    }
    _set_approve_all_preview_operation(operation_token, payload)
    frappe.cache.set_value(
        active_key,
        operation_token,
        expires_in_sec=PREVIEW_OPERATION_TTL_SECONDS,
    )
    try:
        frappe.enqueue(
            "db_connector.api_identity_activation.run_approve_all_preview",
            queue="long",
            timeout=PREVIEW_OPERATION_TIMEOUT_SECONDS,
            enqueue_after_commit=True,
            job_id=f"ccd-approve-all-preview-{operation_token}",
            operation_token=operation_token,
            run_name=run.name,
            requested_by=requested_by,
        )
        frappe.db.commit()
    except Exception:
        frappe.cache.delete_value(active_key)
        frappe.cache.delete_value(_preview_operation_key(operation_token))
        raise
    return {"operation_token": operation_token, "status": "Queued"}


def run_approve_all_preview(
    operation_token: str, run_name: str, requested_by: str
) -> dict[str, Any]:
    """Run the exact approve-all selector without creating or applying a batch."""
    if "System Manager" not in set(frappe.get_roles(requested_by)):
        raise frappe.PermissionError("System Manager role is required")
    frappe.set_user(requested_by)
    payload = _approve_all_preview_operation(operation_token)
    if not payload or payload.get("requested_by") != requested_by:
        raise frappe.PermissionError("Approve-all preview operation is unavailable")
    payload.update(
        {
            "status": "Running",
            "stage": "selecting_components",
            "started_at": str(frappe.utils.now_datetime()),
        }
    )
    _set_approve_all_preview_operation(operation_token, payload)
    try:
        run = _run(run_name)
        selected = _selected_components(run.name)
        payload.update(
            {
                "stage": "checking_safety",
                "processed_components": 0,
                "total_components": len(selected),
            }
        )
        _set_approve_all_preview_operation(operation_token, payload)

        def publish_progress(processed: int, total: int) -> None:
            payload.update(
                {
                    "stage": "checking_safety",
                    "processed_components": int(processed),
                    "total_components": int(total),
                }
            )
            _set_approve_all_preview_operation(operation_token, payload)

        result = _preview_components(
            run,
            selected,
            include_safe_components=False,
            component_detail_limit=PREVIEW_UNSAFE_DETAIL_LIMIT,
            progress_callback=publish_progress,
        )
    except Exception as exc:
        frappe.db.rollback()
        payload.update(
            {
                "status": "Failed",
                "stage": "failed",
                "completed_at": str(frappe.utils.now_datetime()),
                "error": f"{type(exc).__name__}: {str(exc)}"[:1000],
            }
        )
        _set_approve_all_preview_operation(operation_token, payload)
        frappe.log_error(frappe.get_traceback(), "CCD approve-all preview failed")
        raise
    payload.update(
        {
            "status": "Completed",
            "stage": "completed",
            "processed_components": len(selected),
            "total_components": len(selected),
            "completed_at": str(frappe.utils.now_datetime()),
            "result": result,
        }
    )
    _set_approve_all_preview_operation(operation_token, payload)
    return result


@frappe.whitelist()
def get_approve_all_preview(operation_token: str) -> dict[str, Any]:
    """Return one manager's preview status without restarting its scan."""
    _require_manager()
    token = str(operation_token or "").strip()
    if len(token) != 32 or any(
        character not in "0123456789abcdef" for character in token
    ):
        frappe.throw("Invalid approve-all preview operation token")
    payload = _approve_all_preview_operation(token)
    if not payload:
        return {"operation_token": token, "status": "Expired"}
    if payload.get("requested_by") != frappe.session.user:
        frappe.throw("This approve-all preview belongs to another user")
    if payload.get("status") in {"Queued", "Running"}:
        from frappe.utils.background_jobs import get_job_status

        job_status = get_job_status(f"ccd-approve-all-preview-{token}")
        if job_status is None or job_status in {
            "failed",
            "canceled",
            "stopped",
            "finished",
        }:
            payload["status"] = "Unknown"
            payload["error"] = (
                "The background preview ended without a confirmed result. "
                "No identity or batch records were written."
            )
            _set_approve_all_preview_operation(token, payload)
    return payload


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
    progress_callback: Callable[[str, int, int], None] | None = None,
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
    reconciliation = reconcile_materialized_recommendations(run.name)
    if reconciliation["recommendation_count"]:
        _refresh_run_counts(run.name)
        # Persist this terminal lifecycle reconciliation even when no new
        # component remains for the requested batch. Identity state is not
        # written here: no Decision, Group, or Membership is created.
        frappe.db.commit()
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
    if progress_callback:
        progress_callback("checking_safety", 0, len(selected))
    preview = _preview_components(
        run,
        selected,
        progress_callback=(
            (lambda processed, total: progress_callback(
                "checking_safety", processed, total
            ))
            if progress_callback
            else None
        ),
    )
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
    if progress_callback:
        progress_callback("writing_batch", len(selected), len(selected))
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
    """Select the first safe components from bounded, indexed candidate pages."""
    run = _run(run_name)
    limit = int(component_limit or 0)
    if limit < 1 or limit > 100:
        frappe.throw("Automatic component limit must be between 1 and 100")
    selected: list[tuple[str, list[Any]]] = []
    skipped = []
    for page in _automatic_component_pages(run.name):
        page_items = list(page)
        page_summaries = _preview_components(run, page_items)["components"]
        for (component_key, rows), summary in zip(
            page_items, page_summaries, strict=True
        ):
            if summary["safe"]:
                selected.append((component_key, rows))
                if len(selected) >= limit:
                    break
            else:
                skipped.append(summary)
        if len(selected) >= limit:
            break
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


def _creation_operation_key(operation_token: str) -> str:
    return f"ccd_activation_batch_creation:{operation_token}"


def _creation_active_key(
    run_name: str,
    selection_method: str,
    component_limit: int | None,
    is_pilot_wave: int,
    is_demonstration: int,
) -> str:
    values = (run_name, selection_method, component_limit, is_pilot_wave, is_demonstration)
    digest = hashlib.sha256(_json(values).encode()).hexdigest()
    return f"ccd_activation_batch_creation_active:{digest}"


def _creation_operation(operation_token: str) -> dict[str, Any] | None:
    raw = frappe.cache.get_value(_creation_operation_key(operation_token), expires=True)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _set_creation_operation(operation_token: str, payload: dict[str, Any]) -> None:
    frappe.cache.set_value(
        _creation_operation_key(operation_token),
        _json(payload),
        expires_in_sec=CREATION_OPERATION_TTL_SECONDS,
    )


@frappe.whitelist(methods=["POST"])
def start_activation_batch_creation(
    run_name: str,
    selection_method: str = "Explicit Wave",
    component_limit: int | str | None = None,
    is_pilot_wave: int | str = 0,
    is_demonstration: int | str = 0,
) -> dict[str, Any]:
    """Return promptly; build the reviewed batch on the long queue."""
    _require_manager()
    if selection_method not in {"Explicit Wave", "Approve All Eligible"}:
        frappe.throw("Unsupported Desk Activation Batch selection method")
    run = _run(str(run_name))
    limit = int(component_limit) if component_limit not in (None, "") else None
    if selection_method == "Explicit Wave" and (limit is None or limit <= 0):
        frappe.throw("Component limit must be greater than zero")
    if selection_method == "Approve All Eligible" and limit is not None:
        frappe.throw("Approve All Eligible does not accept a component limit")
    pilot = int(is_pilot_wave or 0)
    demonstration = int(is_demonstration or 0)
    if pilot not in {0, 1} or demonstration not in {0, 1}:
        frappe.throw("Invalid Activation Batch flags")
    locked = frappe.db.sql(
        "SELECT name FROM `tabCCD Match Canary Run` WHERE name=%s FOR UPDATE",
        (run.name,),
    )
    if not locked:
        frappe.throw("CCD Match Canary Run no longer exists")
    active_key = _creation_active_key(
        run.name, selection_method, limit, pilot, demonstration
    )
    active_token = frappe.cache.get_value(active_key)
    if isinstance(active_token, bytes):
        active_token = active_token.decode("utf-8", "replace")
    existing = _creation_operation(str(active_token)) if active_token else None
    if existing and existing.get("status") in {"Queued", "Running"}:
        if existing.get("requested_by") != frappe.session.user:
            frappe.throw("Another System Manager is creating this Activation Batch")
        return {
            "operation_token": str(active_token),
            "status": existing["status"],
            "already_running": True,
        }

    operation_token = uuid.uuid4().hex
    requested_by = str(frappe.session.user)
    _set_creation_operation(operation_token, {
        "operation_token": operation_token,
        "run_name": run.name,
        "requested_by": requested_by,
        "status": "Queued",
        "stage": "queued",
        "processed_components": 0,
        "total_components": 0,
        "queued_at": str(frappe.utils.now_datetime()),
    })
    frappe.cache.set_value(
        active_key,
        operation_token,
        expires_in_sec=CREATION_OPERATION_TTL_SECONDS,
    )
    try:
        frappe.enqueue(
            "db_connector.api_identity_activation.run_activation_batch_creation",
            queue="long",
            timeout=CREATION_OPERATION_TIMEOUT_SECONDS,
            enqueue_after_commit=True,
            job_id=f"ccd-activation-batch-{operation_token}",
            operation_token=operation_token,
            run_name=run.name,
            selection_method=selection_method,
            component_limit=limit,
            is_pilot_wave=pilot,
            is_demonstration=demonstration,
            requested_by=requested_by,
        )
        frappe.db.commit()
    except Exception:
        frappe.cache.delete_value(active_key)
        frappe.cache.delete_value(_creation_operation_key(operation_token))
        raise
    return {"operation_token": operation_token, "status": "Queued"}


def run_activation_batch_creation(
    operation_token: str,
    run_name: str,
    selection_method: str,
    component_limit: int | None,
    is_pilot_wave: int,
    is_demonstration: int,
    requested_by: str,
) -> dict[str, Any]:
    """Only plan a batch; this worker never approves or materializes it."""
    if "System Manager" not in set(frappe.get_roles(requested_by)):
        raise frappe.PermissionError("System Manager role is required")
    frappe.set_user(requested_by)
    payload = _creation_operation(operation_token)
    if not payload or payload.get("requested_by") != requested_by:
        raise frappe.PermissionError("Activation Batch creation operation is unavailable")
    payload["status"] = "Running"
    payload["stage"] = "selecting_components"
    payload["processed_components"] = 0
    payload["total_components"] = 0
    payload["started_at"] = str(frappe.utils.now_datetime())
    _set_creation_operation(operation_token, payload)
    try:
        def publish_progress(stage: str, processed: int, total: int) -> None:
            payload.update(
                {
                    "stage": stage,
                    "processed_components": int(processed),
                    "total_components": int(total),
                }
            )
            _set_creation_operation(operation_token, payload)

        result = _create_activation_batch(
            run_name,
            selection_method,
            component_limit,
            None,
            is_pilot_wave,
            is_demonstration,
            progress_callback=publish_progress,
        )
    except Exception as exc:
        frappe.db.rollback()
        payload.update({
            "status": "Failed",
            "completed_at": str(frappe.utils.now_datetime()),
            "error": f"{type(exc).__name__}: {str(exc)}"[:1000],
        })
        _set_creation_operation(operation_token, payload)
        frappe.log_error(frappe.get_traceback(), "CCD Activation Batch creation failed")
        raise
    # _create_activation_batch committed before returning. If publishing the
    # result fails, the outcome is unknown to the UI, never a false failure.
    payload.update({
        "status": "Completed",
        "stage": "completed",
        "completed_at": str(frappe.utils.now_datetime()),
        "batch": result["batch"],
        "batch_status": result["status"],
    })
    _set_creation_operation(operation_token, payload)
    return result


@frappe.whitelist()
def get_activation_batch_creation(operation_token: str) -> dict[str, Any]:
    """Read one manager's queued creation result without repeating the work."""
    _require_manager()
    token = str(operation_token or "").strip()
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        frappe.throw("Invalid Activation Batch operation token")
    payload = _creation_operation(token)
    if not payload:
        return {"operation_token": token, "status": "Expired"}
    if payload.get("requested_by") != frappe.session.user:
        frappe.throw("This Activation Batch operation belongs to another user")
    if payload.get("status") in {"Queued", "Running"}:
        from frappe.utils.background_jobs import get_job_status

        job_status = get_job_status(f"ccd-activation-batch-{token}")
        if job_status is None or job_status in {"failed", "canceled", "stopped", "finished"}:
            # A worker can stop after committing the batch but before its
            # result reaches Redis. Never report that uncertain outcome as a
            # definite creation failure.
            payload["status"] = "Unknown"
            payload["error"] = (
                "The background worker ended without a confirmed result. "
                "Check existing batches before retrying."
            )
            _set_creation_operation(token, payload)
    return payload


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
    retired_records: set[str] = set()
    for recommendation in recommendations:
        payload = _pair_evidence_payload(recommendation)
        sensitive_values_visible = bool(payload["sensitive_values_visible"])
        if payload.get("historical_source_retired"):
            for record_id in (recommendation.left_record, recommendation.right_record):
                if not frappe.db.exists("CCD Master", record_id):
                    retired_records.add(str(record_id))
        payload["left"]["alias"] = aliases[str(recommendation.left_record)]
        payload["right"]["alias"] = aliases[str(recommendation.right_record)]
        pair_payloads.append(payload)

    records = []
    for record_id in sorted(record_sources):
        record = {
            "alias": aliases[record_id],
            "source": record_sources[record_id],
        }
        if sensitive_values_visible and record_id not in retired_records:
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
        "historical_source_retired": bool(retired_records),
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
    component_keys = [
        str(item.component_fingerprint)
        for item in batch.items
        if item.status not in {"Applied", "Already Applied", "Corrected"}
    ]
    components = _component_rows(run.name, component_keys=component_keys)
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


def _apply_approved_recommendation_delta(
    run_name: str, approved_count: int
) -> dict[str, int]:
    """Update exact Canary counters without rescanning the recommendation run.

    Every caller-provided transition is part of the same transaction as its
    recommendation update. The single SQL statement therefore serializes
    concurrent batches on the Canary row and rolls back with materialization.
    A separately queued full recount remains the drift-detection safety net.
    """
    transition_count = int(approved_count or 0)
    if transition_count < 0:
        frappe.throw("Approved recommendation delta cannot be negative")
    if transition_count:
        frappe.db.sql(
            """
            UPDATE `tabCCD Match Canary Run`
               SET proposed_count=GREATEST(COALESCE(proposed_count,0)-%s,0),
                   active_count=COALESCE(active_count,0)+%s
             WHERE name=%s
            """,
            (transition_count, transition_count, run_name),
        )
    values = frappe.db.get_value(
        RUN_DOCTYPE,
        run_name,
        [
            "proposed_count",
            "exception_count",
            "active_count",
            "reversed_count",
            "superseded_count",
        ],
        as_dict=True,
    )
    if not values:
        frappe.throw("CCD Match Canary Run no longer exists")
    return {
        fieldname: int(values.get(fieldname) or 0)
        for fieldname in (
            "proposed_count",
            "exception_count",
            "active_count",
            "reversed_count",
            "superseded_count",
        )
    }


def reconcile_activation_run_counts(
    run_name: str, batch_name: str = ""
) -> dict[str, Any]:
    """Recount a committed activation asynchronously as a safety check."""
    counts = _refresh_run_counts(run_name)
    frappe.db.commit()
    return {"run": run_name, "batch": batch_name, "counts": counts}


def _enqueue_activation_run_count_reconciliation(
    run_name: str, batch_name: str
) -> str | None:
    """Queue the expensive exact recount after Apply is already committed."""
    job_id = f"ccd-canary-count-reconcile-{batch_name}"
    try:
        frappe.enqueue(
            "db_connector.api_identity_activation.reconcile_activation_run_counts",
            queue="long",
            timeout=RUN_COUNT_RECONCILIATION_TIMEOUT_SECONDS,
            enqueue_after_commit=False,
            job_id=job_id,
            deduplicate=True,
            run_name=run_name,
            batch_name=batch_name,
        )
        return job_id
    except Exception:
        # The transaction already committed exact deltas. A failed safety-job
        # enqueue must not turn a successful governed activation into a false
        # failure or invite an operator to apply it twice.
        frappe.log_error(
            frappe.get_traceback(),
            "CCD Canary count reconciliation enqueue failed",
        )
        return None


def _apply_activation_batch(
    batch_name: str,
    *,
    allow_automatic: bool = False,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    _require_manager()
    locked = frappe.db.sql(
        f"SELECT name FROM `tab{BATCH_DOCTYPE}` WHERE name=%s FOR UPDATE",
        (batch_name,),
    )
    if not locked:
        frappe.throw("CCD Identity Activation Batch no longer exists")
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
        approved_recommendation_delta = 0
        component_keys = [
            str(item.component_fingerprint)
            for item in batch.items
            if item.status not in {"Applied", "Already Applied", "Corrected"}
        ]
        total_components = len(component_keys)

        def report_progress(stage: str, processed: int) -> None:
            if not progress_callback:
                return
            try:
                progress_callback(stage, int(processed), total_components)
            except Exception:
                # Progress is operational telemetry, not governed identity
                # state. A Redis/status publication failure must never abort
                # or roll back an otherwise valid materialization.
                try:
                    frappe.log_error(
                        frappe.get_traceback(),
                        "CCD Activation Batch apply progress publication failed",
                    )
                except Exception:
                    pass

        report_progress("loading_components", 0)
        components = _component_rows(run.name, component_keys=component_keys)
        report_progress("materializing_components", 0)
        processed_components = 0
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
                approved_recommendation_delta += 1
            item.db_set(
                {
                    "status": "Applied" if result["status"] == "Applied" else "Already Applied",
                    "identity_decision": result["identity_decision"],
                    "identity_group": group_name or None,
                },
                update_modified=False,
            )
            processed_components += 1
            if (
                processed_components == total_components
                or processed_components % APPLY_PROGRESS_INTERVAL == 0
            ):
                report_progress("materializing_components", processed_components)
        report_progress("finalizing", processed_components)
        counts = _apply_approved_recommendation_delta(
            run.name, approved_recommendation_delta
        )
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
        # Batch/Item planning records intentionally do not trigger dashboard
        # refreshes. Register one marker only after the complete activation is
        # ready to commit; lifecycle hooks raised by the materializer coalesce
        # into this same callback.
        from db_connector.ccd_dashboard_snapshot import mark_dirty_after_commit

        mark_dirty_after_commit(reason="identity-activation-applied")
        report_progress("committing", processed_components)
        frappe.db.commit()
        reconciliation_job = _enqueue_activation_run_count_reconciliation(
            run.name, batch.name
        )
        return {
            "batch": batch.name,
            "status": "Applied",
            "created_groups": created_groups,
            "created_memberships": created_memberships,
            "approved_recommendations": counts["active_count"],
            "run_count_reconciliation_job": reconciliation_job,
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


def _apply_operation_key(operation_token: str) -> str:
    return f"ccd_activation_batch_apply:{operation_token}"


def _apply_active_key(batch_name: str) -> str:
    digest = hashlib.sha256(str(batch_name).encode()).hexdigest()
    return f"ccd_activation_batch_apply_active:{digest}"


def _apply_operation(operation_token: str) -> dict[str, Any] | None:
    raw = frappe.cache.get_value(_apply_operation_key(operation_token), expires=True)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _set_apply_operation(operation_token: str, payload: dict[str, Any]) -> None:
    frappe.cache.set_value(
        _apply_operation_key(operation_token),
        _json(payload),
        expires_in_sec=APPLY_OPERATION_TTL_SECONDS,
    )


def _applied_batch_operation_result(batch_name: str) -> dict[str, Any]:
    values = frappe.db.get_value(
        BATCH_DOCTYPE,
        batch_name,
        [
            "status",
            "created_group_count",
            "created_membership_count",
            "applied_at",
            "applied_by",
            "error_summary",
        ],
        as_dict=True,
    )
    if not values:
        return {"batch": batch_name, "status": "Missing"}
    return {
        "batch": batch_name,
        "status": str(values.status or ""),
        "created_groups": int(values.created_group_count or 0),
        "created_memberships": int(values.created_membership_count or 0),
        "applied_at": str(values.applied_at or ""),
        "applied_by": str(values.applied_by or ""),
        "error": str(values.error_summary or ""),
    }


@frappe.whitelist(methods=["POST"])
def start_activation_batch_apply(batch_name: str) -> dict[str, Any]:
    """Queue one manual Apply and return before the HTTP request can time out."""
    _require_manager()
    batch_name = str(batch_name or "").strip()
    locked = frappe.db.sql(
        f"""
        SELECT name, status, is_automatic, selected_component_count
          FROM `tab{BATCH_DOCTYPE}`
         WHERE name=%s
         FOR UPDATE
        """,
        (batch_name,),
        as_dict=True,
    )
    if not locked:
        frappe.throw("CCD Identity Activation Batch no longer exists")
    batch = locked[0]
    if int(batch.is_automatic or 0):
        frappe.throw(
            "Automatic Tiered batches can be applied only by the governed automation worker"
        )
    if batch.status == "Applied":
        result = _applied_batch_operation_result(batch_name)
        return {"status": "Completed", "already_applied": True, "result": result}
    if batch.status != "Approved":
        frappe.throw("Only an approved Activation Batch may be applied")

    active_key = _apply_active_key(batch_name)
    active_token = frappe.cache.get_value(active_key)
    if isinstance(active_token, bytes):
        active_token = active_token.decode("utf-8", "replace")
    existing = _apply_operation(str(active_token)) if active_token else None
    if existing and existing.get("status") in {"Queued", "Running"}:
        if existing.get("requested_by") != frappe.session.user:
            frappe.throw("Another System Manager is applying this Activation Batch")
        return {
            "operation_token": str(active_token),
            "status": existing["status"],
            "already_running": True,
        }

    operation_token = uuid.uuid4().hex
    requested_by = str(frappe.session.user)
    total_components = int(batch.selected_component_count or 0)
    _set_apply_operation(
        operation_token,
        {
            "operation_token": operation_token,
            "batch": batch_name,
            "requested_by": requested_by,
            "status": "Queued",
            "stage": "queued",
            "processed_components": 0,
            "total_components": total_components,
            "queued_at": str(frappe.utils.now_datetime()),
        },
    )
    frappe.cache.set_value(
        active_key,
        operation_token,
        expires_in_sec=APPLY_OPERATION_TTL_SECONDS,
    )
    try:
        frappe.enqueue(
            "db_connector.api_identity_activation.run_activation_batch_apply",
            queue="long",
            timeout=APPLY_OPERATION_TIMEOUT_SECONDS,
            enqueue_after_commit=True,
            job_id=f"ccd-activation-apply-{operation_token}",
            operation_token=operation_token,
            batch_name=batch_name,
            requested_by=requested_by,
        )
        frappe.db.commit()
    except Exception:
        frappe.cache.delete_value(active_key)
        frappe.cache.delete_value(_apply_operation_key(operation_token))
        raise
    return {"operation_token": operation_token, "status": "Queued"}


def run_activation_batch_apply(
    operation_token: str, batch_name: str, requested_by: str
) -> dict[str, Any]:
    """Apply one approved manual batch on long without weakening atomicity."""
    if "System Manager" not in set(frappe.get_roles(requested_by)):
        raise frappe.PermissionError("System Manager role is required")
    frappe.set_user(requested_by)
    payload = _apply_operation(operation_token)
    if not payload or payload.get("requested_by") != requested_by:
        raise frappe.PermissionError("Activation Batch Apply operation is unavailable")
    payload.update(
        {
            "status": "Running",
            "stage": "loading_components",
            "started_at": str(frappe.utils.now_datetime()),
        }
    )
    _set_apply_operation(operation_token, payload)
    try:
        def publish_progress(stage: str, processed: int, total: int) -> None:
            payload.update(
                {
                    "stage": stage,
                    "processed_components": int(processed),
                    "total_components": int(total),
                }
            )
            _set_apply_operation(operation_token, payload)

        result = _apply_activation_batch(
            batch_name,
            allow_automatic=False,
            progress_callback=publish_progress,
        )
    except Exception as exc:
        frappe.db.rollback()
        payload.update(
            {
                "status": "Failed",
                "stage": "failed",
                "completed_at": str(frappe.utils.now_datetime()),
                "error": f"{type(exc).__name__}: {str(exc)}"[:1000],
            }
        )
        _set_apply_operation(operation_token, payload)
        frappe.cache.delete_value(_apply_active_key(batch_name))
        frappe.log_error(frappe.get_traceback(), "CCD Activation Batch Apply failed")
        raise
    # _apply_activation_batch committed before returning. If this status
    # publication fails, the getter reconciles the committed Applied batch.
    payload.update(
        {
            "status": "Completed",
            "stage": "completed",
            "processed_components": int(payload.get("total_components") or 0),
            "completed_at": str(frappe.utils.now_datetime()),
            "result": result,
        }
    )
    _set_apply_operation(operation_token, payload)
    frappe.cache.delete_value(_apply_active_key(batch_name))
    return result


@frappe.whitelist()
def get_activation_batch_apply(operation_token: str) -> dict[str, Any]:
    """Read one manager's Apply status without repeating materialization."""
    _require_manager()
    token = str(operation_token or "").strip()
    if len(token) != 32 or any(
        character not in "0123456789abcdef" for character in token
    ):
        frappe.throw("Invalid Activation Batch Apply operation token")
    payload = _apply_operation(token)
    if not payload:
        return {"operation_token": token, "status": "Expired"}
    if payload.get("requested_by") != frappe.session.user:
        frappe.throw("This Activation Batch Apply operation belongs to another user")

    result = _applied_batch_operation_result(str(payload.get("batch") or ""))
    if result["status"] == "Applied":
        payload.update(
            {
                "status": "Completed",
                "stage": "completed",
                "result": result,
            }
        )
        _set_apply_operation(token, payload)
        return payload
    if result["status"] == "Failed":
        payload.update(
            {
                "status": "Failed",
                "stage": "failed",
                "error": result.get("error") or "Activation Batch Apply failed",
            }
        )
        _set_apply_operation(token, payload)
        return payload
    if payload.get("status") in {"Queued", "Running"}:
        from frappe.utils.background_jobs import get_job_status

        job_status = get_job_status(f"ccd-activation-apply-{token}")
        if job_status is None or job_status in {
            "failed",
            "canceled",
            "stopped",
            "finished",
        }:
            payload.update(
                {
                    "status": "Unknown",
                    "stage": "unknown",
                    "error": (
                        "The background worker ended without a confirmed result. "
                        "The batch is not Applied; inspect its status before retrying."
                    ),
                    "batch_status": result["status"],
                }
            )
            _set_apply_operation(token, payload)
    return payload


@frappe.whitelist(methods=["POST"])
def apply_activation_batch(batch_name: str) -> dict[str, Any]:
    """Compatibility endpoint: manual Apply now always queues on long."""
    return start_activation_batch_apply(batch_name)


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
