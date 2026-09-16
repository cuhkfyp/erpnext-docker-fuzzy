"""Read-only current and historical Unified Person membership register."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint


MAXIMUM_LIMIT = 5_000


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
    reveal_records = bool(frappe.has_permission("CCD Master", "read"))
    if not reveal_records:
        for index, row in enumerate(rows, start=1):
            row.ccd_master = _("Masked Record {0}").format(index)
            row.governed_source = _("Masked")
            row.source_record_key = _("Masked")
            row.identity_group = ""
    return _columns(reveal_records), rows, None, None, [
        {"value": len(rows), "label": _("Displayed Memberships"), "datatype": "Int"},
        {
            "value": sum(1 for row in rows if row.membership_status == "Active"),
            "label": _("Active Memberships Displayed"),
            "datatype": "Int",
            "indicator": "Green",
        },
    ]


def _columns(reveal_records: bool) -> list[dict[str, Any]]:
    return [
        {"fieldname": "unified_person", "label": _("Unified Person Number"), "fieldtype": "Link", "options": "CCD Unified Person", "width": 175},
        {"fieldname": "person_status", "label": _("Person Status"), "fieldtype": "Data", "width": 115},
        {"fieldname": "canonical_person", "label": _("Resolves To"), "fieldtype": "Link", "options": "CCD Unified Person", "width": 175},
        {"fieldname": "ccd_master", "label": _("CCD Master") if reveal_records else _("Masked Record"), "fieldtype": "Link" if reveal_records else "Data", **({"options": "CCD Master"} if reveal_records else {}), "width": 155},
        {"fieldname": "governed_source", "label": _("Stable CCD Source"), "fieldtype": "Data", "width": 180},
        {"fieldname": "source_record_key", "label": _("Stable Source Record Key"), "fieldtype": "Data", "width": 190},
        {"fieldname": "identity_group", "label": _("Identity Group"), "fieldtype": "Link" if reveal_records else "Data", **({"options": "CCD Identity Group"} if reveal_records else {}), "width": 145},
        {"fieldname": "membership_status", "label": _("Membership Status"), "fieldtype": "Data", "width": 145},
        {"fieldname": "assignment_reason", "label": _("Assignment Reason"), "fieldtype": "Data", "width": 220},
        {"fieldname": "valid_from", "label": _("Valid From"), "fieldtype": "Datetime", "width": 165},
        {"fieldname": "valid_to", "label": _("Valid To"), "fieldtype": "Datetime", "width": 165},
    ]
