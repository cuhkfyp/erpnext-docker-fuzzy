"""Durable Unified Person registry, lineage reconciliation, and backfill.

Unified Person numbers are never written to ``CCD Master``.  This module owns
the separate registry and immutable assignment/alias history.  Small identity
changes reconcile in the caller's transaction; the initial population uses
bounded, restartable bulk batches.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

import frappe

from db_connector.fuzzy_matching.unified_person import (
    UNIFIED_PERSON_MAX_SEQUENCE,
    format_unified_person_number,
    parse_unified_person_number,
    plan_lineage_reassignment,
    recreated_lineage_person,
    source_lineage_key,
)


PERSON_DOCTYPE = "CCD Unified Person"
MEMBERSHIP_DOCTYPE = "CCD Unified Person Membership"
ALIAS_DOCTYPE = "CCD Unified Person Alias"
EVENT_DOCTYPE = "CCD Unified Person Event"
BACKFILL_DOCTYPE = "CCD Unified Person Backfill Run"
SETTINGS_DOCTYPE = "CCD Unified Person Settings"
IDENTITY_MEMBERSHIP_DOCTYPE = "CCD Identity Membership"
IDENTITY_GROUP_DOCTYPE = "CCD Identity Group"
RESOLUTION_SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
CURRENT_IDENTITY_STATUSES = ("Active", "Needs Revalidation")
MAX_BACKFILL_BATCH_SIZE = 5_000
DEFAULT_BACKFILL_BATCH_SIZE = 2_000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _tables_ready() -> bool:
    return bool(frappe.db.exists("DocType", SETTINGS_DOCTYPE)) and all(
        frappe.db.table_exists(doctype)
        for doctype in (
            PERSON_DOCTYPE,
            MEMBERSHIP_DOCTYPE,
            ALIAS_DOCTYPE,
            EVENT_DOCTYPE,
            BACKFILL_DOCTYPE,
        )
    )


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_direct_reader(ccd_master_name: str | None = None) -> None:
    roles = set(frappe.get_roles())
    if not ({"System Manager", "CCD Match Sensitive Reviewer"} & roles):
        frappe.throw("Sensitive identity access is required", frappe.PermissionError)
    if ccd_master_name and not frappe.has_permission(
        "CCD Master", "read", doc=ccd_master_name
    ):
        frappe.throw("You cannot read this CCD Master", frappe.PermissionError)


def _controls_disabled() -> bool:
    settings = frappe.get_single(RESOLUTION_SETTINGS_DOCTYPE)
    return not any(
        bool(settings.get(fieldname))
        for fieldname in (
            "materialization_enabled",
            "automatic_tiered_enabled",
            "automatic_qc_assignment_enabled",
        )
    )


def _lock_names(doctype: str, names: Iterable[str]) -> None:
    ordered = tuple(sorted({str(name) for name in names if str(name)}))
    if not ordered:
        return
    for offset in range(0, len(ordered), 1_000):
        chunk = ordered[offset : offset + 1_000]
        placeholders = ", ".join(["%s"] * len(chunk))
        frappe.db.sql(
            f"SELECT name FROM `tab{doctype}` WHERE name IN ({placeholders}) "
            "ORDER BY name FOR UPDATE",
            chunk,
        )


def _event_key(
    person: str,
    event_type: str,
    reason: str,
    origin_doctype: str,
    origin_document: str,
    nonce: str,
) -> str:
    return hashlib.sha256(
        "\x1f".join(
            (person, event_type, reason, origin_doctype, origin_document, nonce)
        ).encode()
    ).hexdigest()


def _append_event(
    *,
    person: str,
    event_type: str,
    reason: str,
    origin_doctype: str = "",
    origin_document: str = "",
    related_person: str = "",
    ccd_master: str = "",
    identity_group: str = "",
    metadata: dict[str, Any] | None = None,
    nonce: str = "",
) -> str:
    nonce = nonce or frappe.generate_hash(length=20)
    key = _event_key(
        person, event_type, reason, origin_doctype, origin_document, nonce
    )
    existing = frappe.db.get_value(EVENT_DOCTYPE, {"event_key": key}, "name")
    if existing:
        return str(existing)
    row = frappe.get_doc(
        {
            "doctype": EVENT_DOCTYPE,
            "event_key": key,
            "unified_person": person,
            "related_person": related_person or None,
            "ccd_master": ccd_master or None,
            "identity_group": identity_group or None,
            "event_type": event_type,
            "reason": reason,
            "origin_doctype": origin_doctype or None,
            "origin_document": origin_document or None,
            "event_at": frappe.utils.now_datetime(),
            "actor": frappe.session.user,
            "metadata_json": _json(metadata or {}),
        }
    ).insert(ignore_permissions=True)
    return str(row.name)


def _reserve_sequences(count: int) -> tuple[int, ...]:
    count = int(count)
    if count < 1:
        return ()
    frappe.db.sql(
        "SELECT field, value FROM `tabSingles` WHERE doctype=%s AND field=%s "
        "FOR UPDATE",
        (SETTINGS_DOCTYPE, "last_sequence"),
    )
    last = int(
        frappe.db.get_single_value(SETTINGS_DOCTYPE, "last_sequence") or 0
    )
    end = last + count
    if end > UNIFIED_PERSON_MAX_SEQUENCE:
        frappe.throw("The Unified Person nine-digit sequence is exhausted")
    frappe.db.set_single_value(SETTINGS_DOCTYPE, "last_sequence", end)
    return tuple(range(last + 1, end + 1))


def _issue_person(
    *,
    origin_type: str,
    origin_doctype: str = "",
    origin_document: str = "",
    identity_group: str = "",
    reason: str,
) -> str:
    sequence = _reserve_sequences(1)[0]
    number = format_unified_person_number(sequence)
    now = frappe.utils.now_datetime()
    frappe.get_doc(
        {
            "doctype": PERSON_DOCTYPE,
            "unified_person_number": number,
            "sequence_number": sequence,
            "status": "Active",
            "active_member_count": 0,
            "current_identity_group": identity_group or None,
            "issued_at": now,
            "issued_by": frappe.session.user,
            "origin_type": origin_type,
            "origin_doctype": origin_doctype or None,
            "origin_document": origin_document or None,
            "last_reconciled_at": now,
        }
    ).insert(ignore_permissions=True)
    _append_event(
        person=number,
        event_type="Issue",
        reason=reason,
        origin_doctype=origin_doctype,
        origin_document=origin_document,
        identity_group=identity_group,
        nonce=f"issue:{sequence}",
    )
    return number


def _identity_rows(record_ids: Iterable[str]) -> list[Any]:
    ordered = tuple(sorted({str(value) for value in record_ids if str(value)}))
    if not ordered:
        return []
    output: list[Any] = []
    for offset in range(0, len(ordered), 1_000):
        chunk = ordered[offset : offset + 1_000]
        output.extend(
            frappe.get_all(
                IDENTITY_MEMBERSHIP_DOCTYPE,
                filters={
                    "ccd_master": ["in", chunk],
                    "status": ["in", CURRENT_IDENTITY_STATUSES],
                },
                fields=["ccd_master", "identity_group", "status"],
                limit_page_length=100_000,
            )
        )
    return output


def _active_unified_rows(record_ids: Iterable[str]) -> list[Any]:
    ordered = tuple(sorted({str(value) for value in record_ids if str(value)}))
    if not ordered:
        return []
    output: list[Any] = []
    for offset in range(0, len(ordered), 1_000):
        chunk = ordered[offset : offset + 1_000]
        output.extend(
            frappe.get_all(
                MEMBERSHIP_DOCTYPE,
                filters={"ccd_master": ["in", chunk], "status": "Active"},
                fields=[
                    "name",
                    "unified_person",
                    "ccd_master",
                    "identity_group",
                    "status",
                    "valid_from",
                ],
                limit_page_length=100_000,
            )
        )
    return output


def _expand_scope(record_ids: Iterable[str]) -> tuple[str, ...]:
    scope = {str(value) for value in record_ids if str(value)}
    if not scope:
        return ()
    for _ in range(20):
        before = len(scope)
        identity_rows = _identity_rows(scope)
        group_names = sorted({str(row.identity_group) for row in identity_rows})
        for offset in range(0, len(group_names), 500):
            chunk = group_names[offset : offset + 500]
            scope.update(
                str(value)
                for value in frappe.get_all(
                    IDENTITY_MEMBERSHIP_DOCTYPE,
                    filters={
                        "identity_group": ["in", chunk],
                        "status": ["in", CURRENT_IDENTITY_STATUSES],
                    },
                    pluck="ccd_master",
                    limit_page_length=100_000,
                )
            )
        unified_rows = _active_unified_rows(scope)
        people = sorted({str(row.unified_person) for row in unified_rows})
        for offset in range(0, len(people), 500):
            chunk = people[offset : offset + 500]
            scope.update(
                str(value)
                for value in frappe.get_all(
                    MEMBERSHIP_DOCTYPE,
                    filters={"unified_person": ["in", chunk], "status": "Active"},
                    pluck="ccd_master",
                    limit_page_length=100_000,
                )
            )
        if len(scope) == before:
            break
    existing = set()
    ordered = sorted(scope)
    for offset in range(0, len(ordered), 1_000):
        existing.update(
            str(value)
            for value in frappe.get_all(
                "CCD Master",
                filters={"name": ["in", ordered[offset : offset + 1_000]]},
                pluck="name",
                limit_page_length=1_000,
            )
        )
    return tuple(sorted(existing))


def _target_partition(
    record_ids: Iterable[str],
) -> tuple[tuple[tuple[str, ...], ...], dict[str, str]]:
    ordered = tuple(sorted({str(value) for value in record_ids if str(value)}))
    identity_rows = _identity_rows(ordered)
    group_for = {str(row.ccd_master): str(row.identity_group) for row in identity_rows}
    grouped: dict[str, set[str]] = defaultdict(set)
    for record_id in ordered:
        group_name = group_for.get(record_id)
        grouped[group_name or f"singleton:{record_id}"].add(record_id)
    clusters = tuple(sorted(tuple(sorted(values)) for values in grouped.values()))
    return clusters, group_for


def _membership_key(
    person: str, record_id: str, origin_doctype: str, origin_document: str, nonce: str
) -> str:
    return hashlib.sha256(
        "\x1f".join(
            ("unified-person-membership-v1", person, record_id, origin_doctype, origin_document, nonce)
        ).encode()
    ).hexdigest()


def _insert_membership(
    *,
    person: str,
    record_id: str,
    identity_group: str,
    reason: str,
    origin_doctype: str,
    origin_document: str,
    now: Any,
) -> str:
    source = frappe.db.get_value(
        "CCD Master", record_id, ["ccd_reg_source", "ccd_source_key"], as_dict=True
    )
    governed_source = str((source or {}).get("ccd_reg_source") or "")
    source_record_key = str((source or {}).get("ccd_source_key") or "")
    nonce = str(now) + ":" + frappe.generate_hash(length=10)
    row = frappe.get_doc(
        {
            "doctype": MEMBERSHIP_DOCTYPE,
            "membership_key": _membership_key(
                person, record_id, origin_doctype, origin_document, nonce
            ),
            "unified_person": person,
            "ccd_master": record_id,
            "governed_source": governed_source or None,
            "source_record_key": source_record_key or None,
            "source_lineage_key": source_lineage_key(
                governed_source, source_record_key
            ) or None,
            "identity_group": identity_group or None,
            "status": "Active",
            "assignment_reason": reason,
            "origin_doctype": origin_doctype or None,
            "origin_document": origin_document or None,
            "valid_from": now,
        }
    ).insert(ignore_permissions=True)
    _append_event(
        person=person,
        event_type="Assign",
        reason=reason,
        origin_doctype=origin_doctype,
        origin_document=origin_document,
        ccd_master=record_id,
        identity_group=identity_group,
        nonce=str(row.name),
    )
    return str(row.name)


def _end_membership(
    row: Any,
    *,
    reason: str,
    origin_doctype: str,
    origin_document: str,
    now: Any,
) -> None:
    frappe.db.set_value(
        MEMBERSHIP_DOCTYPE,
        row.name,
        {
            "status": "Ended",
            "valid_to": now,
            "ended_reason": reason,
            "ended_by": frappe.session.user,
        },
        update_modified=False,
    )
    _append_event(
        person=str(row.unified_person),
        event_type="End Assignment",
        reason=reason,
        origin_doctype=origin_doctype,
        origin_document=origin_document,
        ccd_master=str(row.ccd_master),
        identity_group=str(row.identity_group or ""),
        nonce=str(row.name),
    )


def _end_active_alias(
    person: str,
    *,
    reason: str,
    now: Any,
) -> None:
    rows = frappe.get_all(
        ALIAS_DOCTYPE,
        filters={"alias_person": person, "status": "Active"},
        fields=["name"],
        limit_page_length=10,
    )
    for row in rows:
        frappe.db.set_value(
            ALIAS_DOCTYPE,
            row.name,
            {"status": "Ended", "valid_to": now, "ended_reason": reason},
            update_modified=False,
        )


def _set_alias(
    alias_person: str,
    canonical_person: str,
    *,
    reason: str,
    origin_doctype: str,
    origin_document: str,
    now: Any,
) -> bool:
    if alias_person == canonical_person:
        return False
    current = frappe.db.get_value(
        ALIAS_DOCTYPE,
        {"alias_person": alias_person, "status": "Active"},
        ["name", "canonical_person"],
        as_dict=True,
    )
    if current and str(current.canonical_person) == canonical_person:
        frappe.db.set_value(
            PERSON_DOCTYPE,
            alias_person,
            {
                "status": "Alias",
                "canonical_person": canonical_person,
                "active_member_count": 0,
                "current_identity_group": None,
                "last_reconciled_at": now,
            },
            update_modified=False,
        )
        return False
    if current:
        frappe.db.set_value(
            ALIAS_DOCTYPE,
            current.name,
            {"status": "Ended", "valid_to": now, "ended_reason": reason},
            update_modified=False,
        )
    key = hashlib.sha256(
        f"alias-v1\x1f{alias_person}\x1f{canonical_person}\x1f{origin_doctype}\x1f{origin_document}\x1f{now}".encode()
    ).hexdigest()
    frappe.get_doc(
        {
            "doctype": ALIAS_DOCTYPE,
            "alias_key": key,
            "alias_person": alias_person,
            "canonical_person": canonical_person,
            "status": "Active",
            "reason": reason,
            "origin_doctype": origin_doctype or None,
            "origin_document": origin_document or None,
            "valid_from": now,
        }
    ).insert(ignore_permissions=True)
    frappe.db.set_value(
        PERSON_DOCTYPE,
        alias_person,
        {
            "status": "Alias",
            "canonical_person": canonical_person,
            "active_member_count": 0,
            "current_identity_group": None,
            "last_reconciled_at": now,
        },
        update_modified=False,
    )
    _append_event(
        person=alias_person,
        related_person=canonical_person,
        event_type="Merge Alias",
        reason=reason,
        origin_doctype=origin_doctype,
        origin_document=origin_document,
        nonce=key,
    )
    return True


def reconcile_unified_person_scope(
    record_ids: Iterable[str],
    *,
    reason: str,
    origin_doctype: str = "",
    origin_document: str = "",
) -> dict[str, Any]:
    """Make Unified Person assignments match the current identity partition."""
    if not _tables_ready():
        return {"status": "Schema Not Ready"}
    scope = _expand_scope(record_ids)
    if not scope:
        return {"status": "No Current Records", "record_count": 0}
    _lock_names("CCD Master", scope)
    active_before = _active_unified_rows(scope)
    _lock_names(MEMBERSHIP_DOCTYPE, [row.name for row in active_before])
    clusters, group_for = _target_partition(scope)
    history: list[Any] = []
    for offset in range(0, len(scope), 1_000):
        history.extend(
            frappe.get_all(
                MEMBERSHIP_DOCTYPE,
                filters={"ccd_master": ["in", scope[offset : offset + 1_000]]},
                fields=["name", "unified_person", "ccd_master", "status"],
                limit_page_length=100_000,
            )
        )
    lineages: dict[str, set[str]] = defaultdict(set)
    for row in history:
        lineages[str(row.unified_person)].add(str(row.ccd_master))
    people = sorted(lineages)
    _lock_names(PERSON_DOCTYPE, people)
    person_rows = {
        str(row.name): row
        for row in frappe.get_all(
            PERSON_DOCTYPE,
            filters={"name": ["in", people]},
            fields=["name", "sequence_number", "status", "canonical_person"],
            limit_page_length=max(len(people), 1),
        )
    } if people else {}
    missing_people = sorted(set(people) - set(person_rows))
    if missing_people:
        frappe.throw(
            "Unified Person Membership references missing registry rows: "
            + ", ".join(missing_people[:5])
        )
    lineage_plan = plan_lineage_reassignment(
        clusters,
        lineages,
        {name: int(row.sequence_number) for name, row in person_rows.items()},
    )
    now = frappe.utils.now_datetime()
    cluster_people = dict(lineage_plan.cluster_people)
    issued = 0
    reactivated = 0
    for index, cluster in enumerate(lineage_plan.clusters):
        if index not in cluster_people:
            identity_groups = {group_for.get(record_id, "") for record_id in cluster}
            identity_groups.discard("")
            group_name = next(iter(identity_groups)) if len(identity_groups) == 1 else ""
            cluster_people[index] = _issue_person(
                origin_type="Split" if history else "Identity Resolution",
                origin_doctype=origin_doctype,
                origin_document=origin_document,
                identity_group=group_name,
                reason=reason,
            )
            issued += 1
            continue
        person = cluster_people[index]
        row = person_rows[person]
        if str(row.status) != "Active" or row.canonical_person:
            _end_active_alias(person, reason=reason, now=now)
            frappe.db.set_value(
                PERSON_DOCTYPE,
                person,
                {
                    "status": "Active",
                    "canonical_person": None,
                    "retired_at": None,
                    "retired_reason": None,
                    "last_reconciled_at": now,
                },
                update_modified=False,
            )
            _append_event(
                person=person,
                event_type="Split Reactivate",
                reason=reason,
                origin_doctype=origin_doctype,
                origin_document=origin_document,
                nonce=f"reactivate:{person}:{now}",
            )
            reactivated += 1

    aliases_created = 0
    for alias_person, cluster_index in lineage_plan.alias_cluster.items():
        aliases_created += int(
            _set_alias(
                alias_person,
                cluster_people[cluster_index],
                reason=reason,
                origin_doctype=origin_doctype,
                origin_document=origin_document,
                now=now,
            )
        )

    desired: dict[str, tuple[str, str]] = {}
    for index, cluster in enumerate(lineage_plan.clusters):
        group_names = {group_for.get(record_id, "") for record_id in cluster}
        group_names.discard("")
        group_name = next(iter(group_names)) if len(group_names) == 1 else ""
        for record_id in cluster:
            desired[record_id] = (cluster_people[index], group_name)

    active_by_record = {str(row.ccd_master): row for row in _active_unified_rows(scope)}
    ended = created = 0
    for record_id in scope:
        person, group_name = desired[record_id]
        current = active_by_record.get(record_id)
        if current and str(current.unified_person) == person:
            if str(current.identity_group or "") != group_name:
                frappe.db.set_value(
                    MEMBERSHIP_DOCTYPE,
                    current.name,
                    "identity_group",
                    group_name or None,
                    update_modified=False,
                )
            continue
        if current:
            _end_membership(
                current,
                reason=reason,
                origin_doctype=origin_doctype,
                origin_document=origin_document,
                now=now,
            )
            ended += 1
        _insert_membership(
            person=person,
            record_id=record_id,
            identity_group=group_name,
            reason=reason,
            origin_doctype=origin_doctype,
            origin_document=origin_document,
            now=now,
        )
        created += 1

    for index, cluster in enumerate(lineage_plan.clusters):
        person = cluster_people[index]
        group_names = {group_for.get(record_id, "") for record_id in cluster}
        group_names.discard("")
        group_name = next(iter(group_names)) if len(group_names) == 1 else None
        count = frappe.db.count(
            MEMBERSHIP_DOCTYPE, {"unified_person": person, "status": "Active"}
        )
        frappe.db.set_value(
            PERSON_DOCTYPE,
            person,
            {
                "status": "Active",
                "canonical_person": None,
                "active_member_count": count,
                "current_identity_group": group_name,
                "last_reconciled_at": now,
                "retired_at": None,
                "retired_reason": None,
            },
            update_modified=False,
        )

    return {
        "status": "Reconciled",
        "record_count": len(scope),
        "person_count": len(lineage_plan.clusters),
        "issued_person_count": issued,
        "reactivated_person_count": reactivated,
        "created_alias_count": aliases_created,
        "ended_membership_count": ended,
        "created_membership_count": created,
        "canonical_people": [cluster_people[index] for index in range(len(clusters))],
    }


def retire_unified_person_records(
    record_ids: Iterable[str],
    *,
    reason: str,
    origin_doctype: str = "",
    origin_document: str = "",
) -> dict[str, Any]:
    """End current Unified Person assignments before CCD source retirement."""
    if not _tables_ready():
        return {"status": "Schema Not Ready"}
    ordered = tuple(sorted({str(value) for value in record_ids if str(value)}))
    rows = _active_unified_rows(ordered)
    _lock_names(MEMBERSHIP_DOCTYPE, [row.name for row in rows])
    people = sorted({str(row.unified_person) for row in rows})
    _lock_names(PERSON_DOCTYPE, people)
    now = frappe.utils.now_datetime()
    for row in rows:
        _end_membership(
            row,
            reason=reason,
            origin_doctype=origin_doctype,
            origin_document=origin_document,
            now=now,
        )
    retired = 0
    for person in people:
        count = frappe.db.count(
            MEMBERSHIP_DOCTYPE, {"unified_person": person, "status": "Active"}
        )
        status = frappe.db.get_value(PERSON_DOCTYPE, person, "status")
        values: dict[str, Any] = {
            "active_member_count": count,
            "last_reconciled_at": now,
        }
        if count == 0 and status == "Active":
            values.update(
                {
                    "status": "Retired",
                    "current_identity_group": None,
                    "retired_at": now,
                    "retired_reason": reason,
                }
            )
            retired += 1
            _append_event(
                person=person,
                event_type="Retire",
                reason=reason,
                origin_doctype=origin_doctype,
                origin_document=origin_document,
                nonce=f"retire:{person}:{origin_document}",
            )
        frappe.db.set_value(
            PERSON_DOCTYPE, person, values, update_modified=False
        )
    return {
        "status": "Retired",
        "ended_membership_count": len(rows),
        "retired_person_count": retired,
    }


def ensure_unified_person_after_insert(doc: Any, method: str | None = None) -> None:
    """Issue a singleton number in the same transaction as a new CCD Master."""
    if not _tables_ready():
        return
    lineage_key = source_lineage_key(
        str(doc.get("ccd_reg_source") or ""),
        str(doc.get("ccd_source_key") or ""),
    )
    if lineage_key:
        prior = frappe.get_all(
            MEMBERSHIP_DOCTYPE,
            filters={"source_lineage_key": lineage_key},
            fields=["name", "unified_person", "ccd_master", "status", "valid_from"],
            order_by="valid_from desc, name desc",
            limit_page_length=100_000,
        )
        active_other = {
            str(row.ccd_master)
            for row in prior
            if str(row.status) == "Active" and str(row.ccd_master) != str(doc.name)
        }
        canonical_candidates = {
            _canonical_person(str(row.unified_person)) for row in prior
        }
        canonical = recreated_lineage_person(canonical_candidates, active_other)
        if prior and canonical:
            now = frappe.utils.now_datetime()
            _lock_names(PERSON_DOCTYPE, [canonical])
            _insert_membership(
                person=canonical,
                record_id=str(doc.name),
                identity_group="",
                reason="ccd_master_recreated_from_stable_source_lineage",
                origin_doctype="CCD Master",
                origin_document=str(doc.name),
                now=now,
            )
            current_count = frappe.db.count(
                MEMBERSHIP_DOCTYPE,
                {"unified_person": canonical, "status": "Active"},
            )
            frappe.db.set_value(
                PERSON_DOCTYPE,
                canonical,
                {
                    "status": "Active",
                    "canonical_person": None,
                    "active_member_count": current_count,
                    "retired_at": None,
                    "retired_reason": None,
                    "last_reconciled_at": now,
                },
                update_modified=False,
            )
            _append_event(
                person=canonical,
                event_type="Reconcile",
                reason="stable_source_lineage_restored_after_recreation",
                origin_doctype="CCD Master",
                origin_document=str(doc.name),
                ccd_master=str(doc.name),
                metadata={"source_lineage_key": lineage_key},
                nonce=f"restore:{lineage_key}:{doc.name}",
            )
            return
        if prior and not canonical:
            frappe.log_error(
                title="CCD Unified Person source lineage requires review",
                message=_json(
                    {
                        "ccd_master": str(doc.name),
                        "source_lineage_key": lineage_key,
                        "active_other_ccd_masters": sorted(active_other),
                        "canonical_candidates": sorted(canonical_candidates),
                        "action": "issued_new_singleton_without_guessing",
                    }
                ),
            )
    reconcile_unified_person_scope(
        [str(doc.name)],
        reason="new_ccd_master_singleton",
        origin_doctype="CCD Master",
        origin_document=str(doc.name),
    )


def _modified_snapshot_sha256() -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = ""
    while True:
        rows = frappe.db.sql(
            "SELECT name, modified FROM `tabCCD Master` WHERE name > %s "
            "ORDER BY name LIMIT 5000",
            (cursor,),
            as_dict=True,
        )
        if not rows:
            break
        for row in rows:
            digest.update(str(row.name).encode())
            digest.update(b"\x1f")
            digest.update(str(row.modified or "").encode())
            digest.update(b"\n")
            count += 1
        cursor = str(rows[-1].name)
    return count, digest.hexdigest()


def _backfill_preview() -> dict[str, Any]:
    master_count, modified_sha256 = _modified_snapshot_sha256()
    current_identity = frappe.db.sql(
        """SELECT COUNT(DISTINCT im.ccd_master) AS member_count,
                  COUNT(DISTINCT im.identity_group) AS group_count
             FROM `tabCCD Identity Membership` im
             JOIN `tabCCD Identity Group` ig ON ig.name=im.identity_group
            WHERE im.status IN ('Active', 'Needs Revalidation')
              AND ig.status IN ('Active', 'Needs Revalidation')""",
        as_dict=True,
    )[0]
    grouped_members = int(current_identity.member_count or 0)
    identity_groups = int(current_identity.group_count or 0)
    active_memberships = frappe.db.count(MEMBERSHIP_DOCTYPE, {"status": "Active"})
    people = frappe.db.count(PERSON_DOCTYPE)
    last_sequence = int(
        frappe.db.get_single_value(SETTINGS_DOCTYPE, "last_sequence") or 0
    )
    expected_people = master_count - grouped_members + identity_groups
    payload = {
        "version": "unified-person-backfill-v1",
        "ccd_master_count": master_count,
        "ccd_master_modified_sha256": modified_sha256,
        "current_identity_group_count": identity_groups,
        "current_identity_grouped_member_count": grouped_members,
        "expected_unified_person_count": expected_people,
        "existing_unified_person_count": people,
        "existing_active_membership_count": active_memberships,
        "last_sequence": last_sequence,
    }
    fingerprint = hashlib.sha256(_json(payload).encode()).hexdigest()
    return {
        "zero_write": True,
        "scope_fingerprint": fingerprint,
        **payload,
        "remaining_ccd_master_count": max(master_count - active_memberships, 0),
        "controls_disabled": _controls_disabled(),
    }


@frappe.whitelist()
def preview_unified_person_backfill() -> dict[str, Any]:
    _require_manager()
    if not _tables_ready():
        frappe.throw("Unified Person schema is not installed")
    return _backfill_preview()


@frappe.whitelist()
def get_unified_person_backfill_status() -> dict[str, Any]:
    _require_manager()
    if not _tables_ready():
        return {"schema_ready": False, "status": "Schema Not Ready"}
    rows = frappe.get_all(
        BACKFILL_DOCTYPE,
        fields=[
            "name", "status", "scope_fingerprint", "batch_size",
            "initial_ccd_master_count", "expected_person_count",
            "processed_record_count", "issued_person_count",
            "created_membership_count", "remaining_record_count", "batch_count",
            "started_at", "last_batch_at", "completed_at",
            "ccd_master_modified_unchanged", "result_json", "error_summary",
        ],
        order_by="started_at desc, name desc",
        limit_page_length=1,
    )
    if not rows:
        return {"schema_ready": True, "status": "Not Started"}
    row = dict(rows[0])
    row["backfill_run"] = row.pop("name")
    if row.get("result_json"):
        try:
            row["result"] = json.loads(row["result_json"])
        except (TypeError, ValueError):
            row["result"] = {}
    row.pop("result_json", None)
    return {"schema_ready": True, **row}


@frappe.whitelist()
def start_unified_person_backfill(
    confirm_scope_fingerprint: str,
    batch_size: int | str = DEFAULT_BACKFILL_BATCH_SIZE,
) -> dict[str, Any]:
    _require_manager()
    if not _controls_disabled():
        frappe.throw(
            "Materialization, Automatic Tiered, and Automatic QC must remain disabled during the initial Unified Person backfill"
        )
    completed_run = frappe.db.get_single_value(SETTINGS_DOCTYPE, "backfill_run")
    if frappe.db.get_single_value(SETTINGS_DOCTYPE, "backfill_completed"):
        return {
            "backfill_run": str(completed_run or ""),
            "status": "Completed",
            "idempotent": True,
        }
    running = frappe.db.get_value(
        BACKFILL_DOCTYPE,
        {"status": "Running"},
        ["name", "scope_fingerprint", "remaining_record_count"],
        order_by="started_at desc",
        as_dict=True,
    )
    if running:
        return {
            "backfill_run": str(running.name),
            "status": "Running",
            "scope_fingerprint": str(running.scope_fingerprint),
            "remaining_record_count": int(running.remaining_record_count or 0),
            "idempotent": True,
        }
    batch_size = int(batch_size or DEFAULT_BACKFILL_BATCH_SIZE)
    if batch_size < 1 or batch_size > MAX_BACKFILL_BATCH_SIZE:
        frappe.throw(
            f"Unified Person batch size must be between 1 and {MAX_BACKFILL_BATCH_SIZE}"
        )
    preview = _backfill_preview()
    if str(confirm_scope_fingerprint or "") != preview["scope_fingerprint"]:
        frappe.throw("The Unified Person population changed; run a fresh preview")
    key = hashlib.sha256(
        f"unified-person-backfill-v1\x1f{preview['scope_fingerprint']}".encode()
    ).hexdigest()
    existing = frappe.db.get_value(
        BACKFILL_DOCTYPE,
        {"backfill_key": key},
        ["name", "status"],
        as_dict=True,
    )
    if existing:
        return {"backfill_run": existing.name, "status": existing.status}
    now = frappe.utils.now_datetime()
    run = frappe.get_doc(
        {
            "doctype": BACKFILL_DOCTYPE,
            "backfill_key": key,
            "status": "Running",
            "scope_fingerprint": preview["scope_fingerprint"],
            "batch_size": batch_size,
            "initial_ccd_master_count": preview["ccd_master_count"],
            "expected_person_count": preview["expected_unified_person_count"],
            "processed_record_count": preview["existing_active_membership_count"],
            "issued_person_count": preview["existing_unified_person_count"],
            "created_membership_count": preview["existing_active_membership_count"],
            "remaining_record_count": preview["remaining_ccd_master_count"],
            "batch_count": 0,
            "initial_modified_sha256": preview["ccd_master_modified_sha256"],
            "started_at": now,
            "started_by": frappe.session.user,
            "preview_json": _json(preview),
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"backfill_run": run.name, "status": "Running", **preview}


def _missing_membership_names(limit: int) -> tuple[str, ...]:
    rows = frappe.db.sql(
        f"""SELECT master.name
              FROM `tabCCD Master` master
              LEFT JOIN `tab{MEMBERSHIP_DOCTYPE}` membership
                ON membership.ccd_master=master.name AND membership.status='Active'
             WHERE membership.name IS NULL
             ORDER BY master.name
             LIMIT %s""",
        (int(limit),),
    )
    return tuple(str(row[0]) for row in rows)


def _missing_membership_count() -> int:
    return int(
        frappe.db.sql(
            f"""SELECT COUNT(*)
                  FROM `tabCCD Master` master
                  LEFT JOIN `tab{MEMBERSHIP_DOCTYPE}` membership
                    ON membership.ccd_master=master.name AND membership.status='Active'
                 WHERE membership.name IS NULL"""
        )[0][0]
        or 0
    )


def _backfill_units(seed_records: Iterable[str]) -> tuple[tuple[tuple[str, ...], str], ...]:
    seeds = tuple(sorted({str(value) for value in seed_records if str(value)}))
    identity_rows = _identity_rows(seeds)
    group_for_seed = {str(row.ccd_master): str(row.identity_group) for row in identity_rows}
    group_names = sorted(set(group_for_seed.values()))
    group_members: dict[str, set[str]] = defaultdict(set)
    for offset in range(0, len(group_names), 500):
        chunk = group_names[offset : offset + 500]
        for row in frappe.get_all(
            IDENTITY_MEMBERSHIP_DOCTYPE,
            filters={
                "identity_group": ["in", chunk],
                "status": ["in", CURRENT_IDENTITY_STATUSES],
            },
            fields=["ccd_master", "identity_group"],
            limit_page_length=100_000,
        ):
            group_members[str(row.identity_group)].add(str(row.ccd_master))
    units: dict[str, tuple[tuple[str, ...], str]] = {}
    for record_id in seeds:
        group_name = group_for_seed.get(record_id, "")
        if group_name:
            units[f"group:{group_name}"] = (
                tuple(sorted(group_members[group_name])),
                group_name,
            )
        else:
            units[f"singleton:{record_id}"] = ((record_id,), "")
    return tuple(units[key] for key in sorted(units))


def _bulk_issue_backfill_units(
    units: Iterable[tuple[tuple[str, ...], str]], run_name: str
) -> tuple[int, int]:
    units = tuple(units)
    sequences = _reserve_sequences(len(units))
    now = frappe.utils.now_datetime()
    actor = frappe.session.user or "Administrator"
    person_fields = [
        "name", "creation", "modified", "owner", "modified_by", "docstatus", "idx",
        "unified_person_number", "sequence_number", "status", "canonical_person",
        "active_member_count", "current_identity_group", "issued_at", "issued_by",
        "origin_type", "origin_doctype", "origin_document", "last_reconciled_at",
    ]
    membership_fields = [
        "name", "creation", "modified", "owner", "modified_by", "docstatus", "idx",
        "membership_key", "unified_person", "ccd_master", "governed_source",
        "source_record_key", "source_lineage_key", "identity_group", "status",
        "assignment_reason", "origin_doctype", "origin_document", "valid_from",
    ]
    person_values = []
    membership_values = []
    all_records = tuple(
        sorted({record_id for records, _group_name in units for record_id in records})
    )
    source_rows: dict[str, Any] = {}
    for offset in range(0, len(all_records), 1_000):
        source_rows.update(
            {
                str(row.name): row
                for row in frappe.get_all(
                    "CCD Master",
                    filters={
                        "name": ["in", all_records[offset : offset + 1_000]]
                    },
                    fields=["name", "ccd_reg_source", "ccd_source_key"],
                    limit_page_length=1_000,
                )
            }
        )
    for sequence, (records, group_name) in zip(sequences, units):
        number = format_unified_person_number(sequence)
        person_values.append(
            (
                number, now, now, actor, actor, 0, 0, number, sequence, "Active", None,
                len(records), group_name or None, now, actor, "Backfill", BACKFILL_DOCTYPE,
                run_name, now,
            )
        )
        for record_id in records:
            key = hashlib.sha256(
                f"unified-person-backfill-v1\x1f{run_name}\x1f{number}\x1f{record_id}".encode()
            ).hexdigest()
            source = source_rows.get(record_id)
            governed_source = str((source or {}).get("ccd_reg_source") or "")
            source_record_key = str((source or {}).get("ccd_source_key") or "")
            membership_values.append(
                (
                    frappe.generate_hash(length=10), now, now, actor, actor, 0, 0,
                    key, number, record_id, governed_source or None,
                    source_record_key or None,
                    source_lineage_key(governed_source, source_record_key) or None,
                    group_name or None, "Active",
                    "initial_unified_person_backfill", BACKFILL_DOCTYPE, run_name, now,
                )
            )
    if person_values:
        frappe.db.bulk_insert(
            PERSON_DOCTYPE, person_fields, person_values, chunk_size=1_000
        )
    if membership_values:
        frappe.db.bulk_insert(
            MEMBERSHIP_DOCTYPE,
            membership_fields,
            membership_values,
            chunk_size=2_000,
        )
    return len(person_values), len(membership_values)


def _complete_backfill(run_name: str) -> dict[str, Any]:
    run = frappe.get_doc(BACKFILL_DOCTYPE, run_name)
    final_count, final_modified = _modified_snapshot_sha256()
    audit = get_unified_person_integrity_audit(_internal=True)
    unchanged = (
        final_count == int(run.initial_ccd_master_count or 0)
        and final_modified == str(run.initial_modified_sha256 or "")
    )
    result = {
        "backfill_run": run.name,
        "status": "Completed",
        "initial_ccd_master_count": int(run.initial_ccd_master_count or 0),
        "final_ccd_master_count": final_count,
        "issued_person_count": frappe.db.count(PERSON_DOCTYPE),
        "active_membership_count": frappe.db.count(
            MEMBERSHIP_DOCTYPE, {"status": "Active"}
        ),
        "integrity_active_issue_count": audit["active_issue_count"],
        "ccd_master_modified_unchanged": unchanged,
        "initial_modified_sha256": run.initial_modified_sha256,
        "final_modified_sha256": final_modified,
    }
    now = frappe.utils.now_datetime()
    frappe.db.set_value(
        BACKFILL_DOCTYPE,
        run.name,
        {
            "status": "Completed",
            "remaining_record_count": 0,
            "final_modified_sha256": final_modified,
            "ccd_master_modified_unchanged": int(unchanged),
            "completed_at": now,
            "completed_by": frappe.session.user,
            "result_json": _json(result),
            "error_summary": None,
        },
        update_modified=False,
    )
    frappe.db.set_single_value(SETTINGS_DOCTYPE, "backfill_completed", 1)
    frappe.db.set_single_value(SETTINGS_DOCTYPE, "backfill_run", run.name)
    frappe.db.set_single_value(SETTINGS_DOCTYPE, "backfill_completed_at", now)
    frappe.db.commit()
    return result


@frappe.whitelist()
def run_unified_person_backfill_batch(backfill_run: str) -> dict[str, Any]:
    _require_manager()
    if not _controls_disabled():
        frappe.throw("All materialization and automation controls must remain disabled")
    run = frappe.get_doc(BACKFILL_DOCTYPE, backfill_run)
    if run.status == "Completed":
        return json.loads(run.result_json or "{}") or {
            "backfill_run": run.name,
            "status": "Completed",
        }
    if run.status != "Running":
        frappe.throw("Only a Running Unified Person backfill can continue")
    seeds = _missing_membership_names(int(run.batch_size))
    if not seeds:
        return _complete_backfill(run.name)
    _lock_names("CCD Master", seeds)
    units = _backfill_units(seeds)
    all_records = tuple(sorted({record for unit, _group in units for record in unit}))
    _lock_names("CCD Master", all_records)
    history_rows = []
    for offset in range(0, len(all_records), 1_000):
        history_rows.extend(
            frappe.get_all(
                MEMBERSHIP_DOCTYPE,
                filters={"ccd_master": ["in", all_records[offset : offset + 1_000]]},
                fields=["ccd_master", "status"],
                limit_page_length=100_000,
            )
        )
    records_with_history = {str(row.ccd_master) for row in history_rows}
    bulk_units = []
    reconcile_units = []
    for unit in units:
        records, _group = unit
        if records_with_history.intersection(records):
            reconcile_units.append(unit)
        else:
            bulk_units.append(unit)
    issued, memberships = _bulk_issue_backfill_units(bulk_units, run.name)
    for records, _group in reconcile_units:
        result = reconcile_unified_person_scope(
            records,
            reason="restartable_unified_person_backfill_reconciliation",
            origin_doctype=BACKFILL_DOCTYPE,
            origin_document=run.name,
        )
        issued += int(result.get("issued_person_count") or 0)
        memberships += int(result.get("created_membership_count") or 0)
    remaining = _missing_membership_count()
    active_count = frappe.db.count(MEMBERSHIP_DOCTYPE, {"status": "Active"})
    person_count = frappe.db.count(PERSON_DOCTYPE)
    frappe.db.set_value(
        BACKFILL_DOCTYPE,
        run.name,
        {
            "processed_record_count": active_count,
            "issued_person_count": person_count,
            "created_membership_count": active_count,
            "remaining_record_count": remaining,
            "batch_count": int(run.batch_count or 0) + 1,
            "last_batch_at": frappe.utils.now_datetime(),
            "error_summary": None,
        },
        update_modified=False,
    )
    frappe.db.commit()
    if remaining == 0:
        return _complete_backfill(run.name)
    return {
        "backfill_run": run.name,
        "status": "Running",
        "batch_issued_people": issued,
        "batch_created_memberships": memberships,
        "active_membership_count": active_count,
        "issued_person_count": person_count,
        "remaining_record_count": remaining,
        "batch_count": int(run.batch_count or 0) + 1,
    }


@frappe.whitelist()
def run_unified_person_backfill_batches(
    backfill_run: str, max_batches: int | str = 10
) -> dict[str, Any]:
    _require_manager()
    count = int(max_batches or 10)
    if count < 1 or count > 25:
        frappe.throw("Run between 1 and 25 bounded batches per request")
    result: dict[str, Any] = {}
    for _ in range(count):
        result = run_unified_person_backfill_batch(backfill_run)
        if result.get("status") == "Completed":
            break
    return result


def _canonical_person(number: str) -> str:
    current = str(number)
    seen = set()
    for _ in range(100):
        if current in seen:
            frappe.throw("Unified Person alias cycle detected")
        seen.add(current)
        row = frappe.db.get_value(
            PERSON_DOCTYPE,
            current,
            ["status", "canonical_person"],
            as_dict=True,
        )
        if not row:
            frappe.throw("Unified Person number does not exist")
        if str(row.status) != "Alias" or not row.canonical_person:
            return current
        current = str(row.canonical_person)
    frappe.throw("Unified Person alias chain exceeds the safety limit")


def unified_person_payload_for_record(ccd_master_name: str) -> dict[str, Any]:
    if not _tables_ready():
        return {}
    row = frappe.db.get_value(
        MEMBERSHIP_DOCTYPE,
        {"ccd_master": ccd_master_name, "status": "Active"},
        ["unified_person", "valid_from"],
        as_dict=True,
    )
    if not row:
        return {}
    canonical = _canonical_person(str(row.unified_person))
    aliases = frappe.get_all(
        ALIAS_DOCTYPE,
        filters={"canonical_person": canonical, "status": "Active"},
        pluck="alias_person",
        order_by="valid_from asc",
        limit_page_length=100_000,
    )
    return {
        "unified_person_number": canonical,
        "unified_person_aliases": sorted(str(value) for value in aliases),
        "unified_person_assigned_at": str(row.valid_from or ""),
    }


@frappe.whitelist()
def get_unified_person_for_ccd_master(ccd_master_name: str) -> dict[str, Any]:
    _require_direct_reader(ccd_master_name)
    if not frappe.db.exists("CCD Master", ccd_master_name):
        frappe.throw("CCD Master does not exist")
    payload = unified_person_payload_for_record(ccd_master_name)
    if not payload:
        return {"ccd_master": ccd_master_name, "status": "Not Assigned"}
    return {"ccd_master": ccd_master_name, "status": "Assigned", **payload}


@frappe.whitelist()
def resolve_unified_person_number(unified_person_number: str) -> dict[str, Any]:
    _require_direct_reader()
    parse_unified_person_number(unified_person_number)
    canonical = _canonical_person(unified_person_number)
    person = frappe.get_doc(PERSON_DOCTYPE, canonical)
    aliases = frappe.get_all(
        ALIAS_DOCTYPE,
        filters={"canonical_person": canonical, "status": "Active"},
        pluck="alias_person",
        order_by="valid_from asc",
        limit_page_length=100_000,
    )
    members = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={"unified_person": canonical, "status": "Active"},
        fields=["ccd_master", "identity_group", "valid_from"],
        order_by="ccd_master",
        limit_page_length=100_000,
    )
    return {
        "requested_number": unified_person_number,
        "canonical_number": canonical,
        "requested_is_alias": unified_person_number != canonical,
        "status": person.status,
        "aliases": sorted(str(value) for value in aliases),
        "active_member_count": len(members),
        "members": [dict(row) for row in members],
    }


def get_unified_person_integrity_audit(*, _internal: bool = False) -> dict[str, Any]:
    if not _internal:
        _require_manager()
    if not _tables_ready():
        return {"active_issue_count": 0, "schema_ready": False}
    missing_master = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM `tab{MEMBERSHIP_DOCTYPE}` membership
                  LEFT JOIN `tabCCD Master` master ON master.name=membership.ccd_master
                 WHERE membership.status='Active' AND master.name IS NULL"""
        )[0][0]
        or 0
    )
    missing_assignment = _missing_membership_count()
    duplicate_assignment = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM (
                    SELECT ccd_master FROM `tab{MEMBERSHIP_DOCTYPE}`
                     WHERE status='Active' GROUP BY ccd_master HAVING COUNT(*) > 1
                ) duplicate_rows"""
        )[0][0]
        or 0
    )
    noncanonical_assignment = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM `tab{MEMBERSHIP_DOCTYPE}` membership
                  JOIN `tab{PERSON_DOCTYPE}` person ON person.name=membership.unified_person
                 WHERE membership.status='Active' AND person.status!='Active'"""
        )[0][0]
        or 0
    )
    group_split = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM (
                    SELECT identity_membership.identity_group
                      FROM `tab{IDENTITY_MEMBERSHIP_DOCTYPE}` identity_membership
                      JOIN `tab{MEMBERSHIP_DOCTYPE}` unified_membership
                        ON unified_membership.ccd_master=identity_membership.ccd_master
                       AND unified_membership.status='Active'
                     WHERE identity_membership.status IN ('Active', 'Needs Revalidation')
                     GROUP BY identity_membership.identity_group
                    HAVING COUNT(DISTINCT unified_membership.unified_person) > 1
                ) split_groups"""
        )[0][0]
        or 0
    )
    count_mismatch = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM `tab{PERSON_DOCTYPE}` person
                  LEFT JOIN (
                    SELECT unified_person, COUNT(*) AS member_count
                      FROM `tab{MEMBERSHIP_DOCTYPE}` WHERE status='Active'
                     GROUP BY unified_person
                  ) actual ON actual.unified_person=person.name
                 WHERE person.status='Active'
                   AND person.active_member_count != COALESCE(actual.member_count, 0)"""
        )[0][0]
        or 0
    )
    alias_mismatch = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM `tab{PERSON_DOCTYPE}` person
                  LEFT JOIN `tab{ALIAS_DOCTYPE}` alias_row
                    ON alias_row.alias_person=person.name AND alias_row.status='Active'
                 WHERE (person.status='Alias' AND (
                            person.canonical_person IS NULL OR alias_row.name IS NULL
                       )) OR (person.status='Active' AND person.canonical_person IS NOT NULL)"""
        )[0][0]
        or 0
    )
    duplicate_active_source_lineage = int(
        frappe.db.sql(
            f"""SELECT COUNT(*) FROM (
                    SELECT source_lineage_key
                      FROM `tab{MEMBERSHIP_DOCTYPE}`
                     WHERE status='Active'
                       AND COALESCE(source_lineage_key, '') != ''
                     GROUP BY source_lineage_key
                    HAVING COUNT(DISTINCT ccd_master) > 1
                ) duplicate_lineages"""
        )[0][0]
        or 0
    )
    invalid_numbers = 0
    cursor = ""
    while True:
        rows = frappe.db.sql(
            f"""SELECT name, sequence_number FROM `tab{PERSON_DOCTYPE}`
                  WHERE name > %s ORDER BY name LIMIT 5000""",
            (cursor,),
            as_dict=True,
        )
        if not rows:
            break
        for row in rows:
            try:
                sequence = parse_unified_person_number(str(row.name))
            except ValueError:
                invalid_numbers += 1
            else:
                invalid_numbers += int(sequence != int(row.sequence_number or 0))
        cursor = str(rows[-1].name)
    counts = {
        "active_membership_missing_ccd_master": missing_master,
        "current_ccd_master_missing_membership": missing_assignment,
        "duplicate_active_membership": duplicate_assignment,
        "active_membership_to_noncanonical_person": noncanonical_assignment,
        "identity_group_spans_multiple_unified_people": group_split,
        "person_active_member_count_mismatch": count_mismatch,
        "alias_state_mismatch": alias_mismatch,
        "duplicate_active_source_lineage": duplicate_active_source_lineage,
        "invalid_number_or_sequence": invalid_numbers,
    }
    return {
        "schema_ready": True,
        "zero_write": True,
        "active_issue_count": sum(counts.values()),
        "active_issue_counts": counts,
        "unified_person_count": frappe.db.count(PERSON_DOCTYPE),
        "active_membership_count": frappe.db.count(
            MEMBERSHIP_DOCTYPE, {"status": "Active"}
        ),
        "active_alias_count": frappe.db.count(ALIAS_DOCTYPE, {"status": "Active"}),
    }


@frappe.whitelist()
def get_unified_person_integrity_report() -> dict[str, Any]:
    return get_unified_person_integrity_audit()


def run_scheduled_unified_person_integrity() -> dict[str, Any]:
    """Repair missed raw-import hooks in a bounded batch, then audit."""
    if not _tables_ready():
        return {"schema_ready": False}
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if settings.backfill_completed:
        missing = _missing_membership_names(500)
        for record_id in missing:
            reconcile_unified_person_scope(
                [record_id],
                reason="scheduled_missing_singleton_reconciliation",
                origin_doctype="CCD Unified Person Settings",
                origin_document=SETTINGS_DOCTYPE,
            )
        if missing:
            frappe.db.commit()
    audit = get_unified_person_integrity_audit(_internal=True)
    if audit.get("active_issue_count"):
        frappe.log_error(
            title="CCD Unified Person integrity alert",
            message=_json(audit),
        )
    return audit
