"""Safe repair for snapshot fingerprints added after legacy matching runs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe

from db_connector.api_identity_resolution import materialization_enabled
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.policy import MatchingPolicy


ROUTES = (
    {
        "label": "tiered_recommendations",
        "doctype": "CCD Match Recommendation",
        "parent_field": "canary_run",
        "parent_doctype": "CCD Match Canary Run",
    },
    {
        "label": "splink_candidates",
        "doctype": "CCD Match Review Candidate",
        "parent_field": "queue_run",
        "parent_doctype": "CCD Match Review Queue Run",
    },
)

SNAPSHOT_FIELDS = [
    "name",
    "left_record",
    "right_record",
    "left_source",
    "right_source",
    "left_modified_at",
    "right_modified_at",
    "left_identity_fingerprint",
    "right_identity_fingerprint",
]


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _snapshot_hash(value: str | dict[str, Any]) -> str:
    parsed = json.loads(value) if isinstance(value, str) else value
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _missing_snapshot_rows(route: dict[str, str]) -> list[Any]:
    if not frappe.db.table_exists(route["doctype"]):
        return []
    rows = frappe.get_all(
        route["doctype"],
        fields=[*SNAPSHOT_FIELDS, route["parent_field"]],
        limit_page_length=100_000,
    )
    return [
        row
        for row in rows
        if not str(row.left_identity_fingerprint or "").strip()
        or not str(row.right_identity_fingerprint or "").strip()
    ]


def _lock_records(record_ids: set[str]) -> None:
    ordered = sorted(record_ids)
    for index in range(0, len(ordered), 500):
        chunk = ordered[index : index + 500]
        placeholders = ", ".join(["%s"] * len(chunk))
        frappe.db.sql(
            f"SELECT name FROM `tabCCD Master` WHERE name IN ({placeholders}) "
            "ORDER BY name FOR UPDATE",
            chunk,
        )


def _current_records(record_ids: set[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    ordered = sorted(record_ids)
    for index in range(0, len(ordered), 250):
        chunk = ordered[index : index + 250]
        rows = frappe.get_all(
            "CCD Master",
            filters={"name": ["in", chunk]},
            fields=["*"],
            limit_page_length=len(chunk),
        )
        output.update({str(row.name): dict(row) for row in rows})
    return output


def _parent_policies(route: dict[str, str], rows: list[Any]) -> dict[str, MatchingPolicy]:
    parent_names = sorted(
        {str(row.get(route["parent_field"]) or "") for row in rows} - {""}
    )
    parents = frappe.get_all(
        route["parent_doctype"],
        filters={"name": ["in", parent_names]},
        fields=["name", "policy_snapshot_json", "policy_snapshot_sha256"],
        limit_page_length=max(len(parent_names), 1),
    )
    output: dict[str, MatchingPolicy] = {}
    for parent in parents:
        snapshot = str(parent.policy_snapshot_json or "")
        if not snapshot or _snapshot_hash(snapshot) != str(
            parent.policy_snapshot_sha256 or ""
        ):
            continue
        output[str(parent.name)] = MatchingPolicy.from_dict(json.loads(snapshot))
    return output


def _route_plan(
    route: dict[str, str], rows: list[Any], records: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    policies = _parent_policies(route, rows)
    fingerprint_cache: dict[tuple[str, str, str], str] = {}
    updates: dict[str, dict[str, str]] = {}
    counts = {
        "missing_rows": len(rows),
        "eligible_rows": 0,
        "stale_rows": 0,
        "corrupt_parent_rows": 0,
        "inconsistent_existing_rows": 0,
        "updated_rows": 0,
    }
    for row in rows:
        parent_name = str(row.get(route["parent_field"]) or "")
        policy = policies.get(parent_name)
        if not policy:
            counts["corrupt_parent_rows"] += 1
            continue

        endpoints = (
            (
                "left_identity_fingerprint",
                str(row.left_record),
                str(row.left_source or ""),
                str(row.left_modified_at or ""),
                str(row.left_identity_fingerprint or "").strip(),
            ),
            (
                "right_identity_fingerprint",
                str(row.right_record),
                str(row.right_source or ""),
                str(row.right_modified_at or ""),
                str(row.right_identity_fingerprint or "").strip(),
            ),
        )
        computed: dict[str, str] = {}
        stale = False
        for fieldname, record_id, frozen_source, frozen_modified, existing in endpoints:
            record = records.get(record_id)
            if (
                not record
                or str(record.get("modified") or "") != frozen_modified
                or str(record.get("ccd_reg_source") or "") != frozen_source
            ):
                stale = True
                break
            cache_key = (parent_name, record_id, frozen_source)
            if cache_key not in fingerprint_cache:
                fingerprint_record = dict(record)
                fingerprint_record["source"] = frozen_source
                fingerprint_cache[cache_key] = identity_fingerprint(
                    fingerprint_record, policy
                )
            fingerprint = fingerprint_cache[cache_key]
            if existing and existing != fingerprint:
                stale = True
                counts["inconsistent_existing_rows"] += 1
                break
            computed[fieldname] = existing or fingerprint
        if stale:
            counts["stale_rows"] += 1
            continue
        counts["eligible_rows"] += 1
        updates[str(row.name)] = computed

    counts["updated_rows"] = len(updates)
    return updates, counts


def _backfill(*, apply: bool) -> dict[str, Any]:
    route_rows = [(route, _missing_snapshot_rows(route)) for route in ROUTES]
    record_ids = {
        str(record_id)
        for _route, rows in route_rows
        for row in rows
        for record_id in (row.left_record, row.right_record)
    }
    if apply:
        _lock_records(record_ids)
    records = _current_records(record_ids)

    route_results = {}
    pending_updates = []
    for route, rows in route_rows:
        updates, counts = _route_plan(route, rows, records)
        route_results[route["label"]] = counts
        pending_updates.append((route["doctype"], updates))

    if apply:
        for doctype, updates in pending_updates:
            frappe.db.bulk_update(
                doctype,
                updates,
                chunk_size=250,
                update_modified=False,
            )
        frappe.db.commit()
    return {
        "zero_write": not apply,
        "applied": apply,
        "routes": route_results,
        "totals": {
            key: sum(route[key] for route in route_results.values())
            for key in (
                "missing_rows",
                "eligible_rows",
                "stale_rows",
                "corrupt_parent_rows",
                "inconsistent_existing_rows",
                "updated_rows",
            )
        },
    }


@frappe.whitelist()
def preview_legacy_identity_fingerprint_backfill() -> dict[str, Any]:
    """Report the bounded legacy repair without writing anything."""
    _require_manager()
    return _backfill(apply=False)


@frappe.whitelist()
def apply_legacy_identity_fingerprint_backfill() -> dict[str, Any]:
    """Backfill only timestamp-current legacy snapshots under their frozen policy."""
    _require_manager()
    if materialization_enabled(automated=False):
        frappe.throw("Disable live Identity Materialization before backfilling")
    return _backfill(apply=True)
