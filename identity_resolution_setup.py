"""Idempotent schema/UI setup for the CCD Identity Resolution workflow."""

from __future__ import annotations

import frappe


def _identity_custom_fields() -> dict[str, list[dict[str, object]]]:
    meta = frappe.get_meta("CCD Master")
    fieldnames = {field.fieldname for field in meta.fields}
    insert_after = next(
        (
            fieldname
            for fieldname in ("btn_match", "match_table", "is_matched", "ccd_source_key")
            if fieldname in fieldnames
        ),
        meta.fields[-1].fieldname if meta.fields else "",
    )
    return {
        "CCD Master": [
            {
                "fieldname": "ccd_identity_resolution_tab",
                "fieldtype": "Tab Break",
                "label": "Identity Resolution",
                "insert_after": insert_after,
            },
            {
                "fieldname": "ccd_identity_resolution_html",
                "fieldtype": "HTML",
                "label": "Reversible Identity Group",
                "insert_after": "ccd_identity_resolution_tab",
                "read_only": 1,
            },
        ]
    }


def _add_indexes() -> None:
    indexes = (
        ("CCD Identity Membership", ["ccd_master", "status"], "ccd_identity_member_current"),
        ("CCD Identity Membership", ["identity_group", "status"], "ccd_identity_group_current"),
        ("CCD Identity Exclusion", ["left_record", "right_record", "status"], "ccd_identity_exclusion_pair"),
        ("CCD Identity Event", ["entity_doctype", "entity_name", "event_at"], "ccd_identity_event_entity"),
    )
    for doctype, fields, index_name in indexes:
        if frappe.db.table_exists(doctype):
            frappe.db.add_index(doctype, fields, index_name=index_name)


def _migrate_recommendation_terms() -> dict[str, int]:
    if not frappe.db.table_exists("CCD Match Recommendation"):
        return {"active_to_approved": 0, "reversed_to_withdrawn": 0}
    active = frappe.db.count("CCD Match Recommendation", {"status": "Active"})
    reversed_count = frappe.db.count("CCD Match Recommendation", {"status": "Reversed"})
    if active:
        frappe.db.sql(
            "UPDATE `tabCCD Match Recommendation` SET status = 'Approved' WHERE status = 'Active'"
        )
    if reversed_count:
        frappe.db.sql(
            "UPDATE `tabCCD Match Recommendation` SET status = 'Withdrawn' WHERE status = 'Reversed'"
        )
    frappe.db.sql(
        "UPDATE `tabCCD Match Recommendation` SET rollout_state = 'Available' "
        "WHERE COALESCE(rollout_state, '') = ''"
    )
    if frappe.db.table_exists("CCD Match Canary Run"):
        from db_connector.api_fuzzy_canary import _refresh_run_counts

        for run_name in frappe.get_all(
            "CCD Match Canary Run", pluck="name", limit_page_length=10_000
        ):
            _refresh_run_counts(run_name)
    return {
        "active_to_approved": int(active),
        "reversed_to_withdrawn": int(reversed_count),
    }


def install_identity_resolution() -> dict[str, object]:
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(_identity_custom_fields(), update=True)
    _add_indexes()
    migration = _migrate_recommendation_terms()
    # Reading the Single creates no business data and preserves the default-off
    # materialization switch established in the DocType schema.
    settings = frappe.get_single("CCD Identity Resolution Settings")
    return {
        "custom_fields": [
            "CCD Master-ccd_identity_resolution_tab",
            "CCD Master-ccd_identity_resolution_html",
        ],
        "materialization_enabled": bool(settings.materialization_enabled),
        "recommendation_term_migration": migration,
    }


def after_migrate() -> None:
    install_identity_resolution()
