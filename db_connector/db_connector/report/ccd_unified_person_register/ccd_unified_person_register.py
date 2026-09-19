"""Read-only current and historical Unified Person membership register."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint


MAXIMUM_LIMIT = 5_000
CURRENT_IDENTITY_STATUSES = ("Active", "Needs Revalidation")
LOOKUP_CHUNK_SIZE = 500


def execute(filters: dict[str, Any] | None = None):
    roles = set(frappe.get_roles())
    if not ({"System Manager", "CCD Match Sensitive Reviewer"} & roles):
        frappe.throw(_("Sensitive identity access is required"), frappe.PermissionError)
    values = frappe._dict(filters or {})
    limit = cint(values.get("limit") or 500)
    if limit < 1 or limit > MAXIMUM_LIMIT:
        frappe.throw(_("Maximum Rows must be between 1 and {0}").format(MAXIMUM_LIMIT))

    conditions = []
    parameters: dict[str, Any] = {"limit": limit}
    for fieldname, column in (
        ("unified_person", "membership.unified_person"),
        ("ccd_master", "membership.ccd_master"),
        ("governed_source", "membership.governed_source"),
        ("person_status", "person.status"),
        ("membership_status", "membership.status"),
    ):
        if values.get(fieldname):
            conditions.append(f"{column}=%({fieldname})s")
            parameters[fieldname] = values[fieldname]
    where = " AND " + " AND ".join(conditions) if conditions else ""
    rows = frappe.db.sql(
        f"""SELECT membership.unified_person,
                   person.status AS person_status,
                   person.canonical_person,
                   membership.ccd_master,
                   membership.governed_source,
                   membership.source_record_key,
                   membership.identity_group,
                   membership.status AS membership_status,
                   membership.assignment_reason,
                   membership.valid_from,
                   membership.valid_to
              FROM `tabCCD Unified Person Membership` membership
              JOIN `tabCCD Unified Person` person
                ON person.name=membership.unified_person
             WHERE 1=1 {where}
             ORDER BY membership.valid_from DESC, membership.name DESC
             LIMIT %(limit)s""",
        parameters,
        as_dict=True,
    )
    _add_identity_group_context(rows)
    reveal_records = bool(frappe.has_permission("CCD Master", "read"))
    if not reveal_records:
        for index, row in enumerate(rows, start=1):
            row.ccd_master = _("Masked Record {0}").format(index)
            row.governed_source = _("Masked")
            row.source_record_key = _("Masked")
            row.identity_group = ""
            row.identity_group_status = ""
            row.current_identity_group = ""
            row.current_identity_group_status = ""
    return _columns(reveal_records), rows, None, None, [
        {"value": len(rows), "label": _("Displayed Memberships"), "datatype": "Int"},
        {
            "value": sum(1 for row in rows if row.membership_status == "Active"),
            "label": _("Active Memberships Displayed"),
            "datatype": "Int",
            "indicator": "Green",
        },
    ]


def _chunks(values: list[str], size: int = LOOKUP_CHUNK_SIZE):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _add_identity_group_context(rows: list[Any]) -> None:
    """Separate assignment-time group provenance from current active groups."""
    record_ids = sorted({str(row.ccd_master) for row in rows if row.ccd_master})
    current_memberships = []
    for chunk in _chunks(record_ids):
        current_memberships.extend(
            frappe.get_all(
                "CCD Identity Membership",
                filters={
                    "ccd_master": ["in", chunk],
                    "status": ["in", CURRENT_IDENTITY_STATUSES],
                },
                fields=["name", "ccd_master", "identity_group", "valid_from"],
                order_by="valid_from desc, name desc",
                limit_page_length=100_000,
            )
        )

    group_ids = {
        str(row.identity_group) for row in rows if row.identity_group
    } | {
        str(membership.identity_group)
        for membership in current_memberships
        if membership.identity_group
    }
    group_statuses = {}
    for chunk in _chunks(sorted(group_ids)):
        for group in frappe.get_all(
            "CCD Identity Group",
            filters={"name": ["in", chunk]},
            fields=["name", "status"],
            limit_page_length=100_000,
        ):
            group_statuses[str(group.name)] = str(group.status or "")

    current_by_record: dict[str, list[str]] = {}
    for membership in current_memberships:
        group = str(membership.identity_group or "")
        if not group or group_statuses.get(group) not in CURRENT_IDENTITY_STATUSES:
            continue
        groups = current_by_record.setdefault(str(membership.ccd_master), [])
        if group not in groups:
            groups.append(group)

    for row in rows:
        origin_group = str(row.identity_group or "")
        current_groups = current_by_record.get(str(row.ccd_master), [])
        row.identity_group_status = group_statuses.get(origin_group, "")
        row.current_identity_group = ",".join(current_groups)
        row.current_identity_group_status = ",".join(
            group_statuses[group] for group in current_groups
        )


def _columns(reveal_records: bool) -> list[dict[str, Any]]:
    return [
        {"fieldname": "unified_person", "label": _("Unified Person Number"), "fieldtype": "Link", "options": "CCD Unified Person", "width": 175},
        {"fieldname": "person_status", "label": _("Person Status"), "fieldtype": "Data", "width": 115},
        {"fieldname": "canonical_person", "label": _("Resolves To"), "fieldtype": "Link", "options": "CCD Unified Person", "width": 175},
        {"fieldname": "ccd_master", "label": _("CCD Master") if reveal_records else _("Masked Record"), "fieldtype": "Link" if reveal_records else "Data", **({"options": "CCD Master"} if reveal_records else {}), "width": 155},
        {"fieldname": "governed_source", "label": _("Stable CCD Source"), "fieldtype": "Data", "width": 180},
        {"fieldname": "source_record_key", "label": _("Stable Source Record Key"), "fieldtype": "Data", "width": 190},
        {"fieldname": "identity_group", "label": _("Origin Identity Group"), "fieldtype": "Data", "width": 210},
        {"fieldname": "current_identity_group", "label": _("Current Identity Group"), "fieldtype": "Data", "width": 210},
        {"fieldname": "membership_status", "label": _("Membership Status"), "fieldtype": "Data", "width": 145},
        {"fieldname": "assignment_reason", "label": _("Assignment Reason"), "fieldtype": "Data", "width": 220},
        {"fieldname": "valid_from", "label": _("Valid From"), "fieldtype": "Datetime", "width": 165},
        {"fieldname": "valid_to", "label": _("Valid To"), "fieldtype": "Datetime", "width": 165},
    ]
