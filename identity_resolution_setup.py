"""Idempotent schema/UI setup for the CCD Identity Resolution workflow."""

from __future__ import annotations

import frappe


IDENTITY_CLIENT_SCRIPT = "CCD Master Identity Resolution"


def _install_identity_client_script() -> dict[str, object]:
    """Load the CCD Master renderer through the path supported by custom DocTypes."""
    script = frappe.read_file(
        frappe.get_app_path(
            "db_connector",
            "public",
            "js",
            "ccd_master_identity_resolution.js",
        )
    )
    # FormMeta.add_code() intentionally skips doctype_js hooks for custom
    # DocTypes.  CCD Master is custom on this site, so an enabled Client Script
    # is required for the renderer to reach the browser.  Keep it disabled on a
    # future site where CCD Master is standard; the existing doctype_js hook is
    # the correct path there and loading both would register duplicate handlers.
    enabled = bool(frappe.db.get_value("DocType", "CCD Master", "custom"))
    values = {
        "dt": "CCD Master",
        "view": "Form",
        "enabled": enabled,
        "script": script,
    }
    created = not frappe.db.exists("Client Script", IDENTITY_CLIENT_SCRIPT)
    if created:
        frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": IDENTITY_CLIENT_SCRIPT,
                **values,
            }
        ).insert(ignore_permissions=True)
    else:
        client_script = frappe.get_doc("Client Script", IDENTITY_CLIENT_SCRIPT)
        changed = any(
            client_script.get(fieldname) != value
            for fieldname, value in values.items()
        )
        if changed:
            client_script.update(values)
            client_script.save(ignore_permissions=True)
    frappe.clear_cache(doctype="CCD Master")
    return {
        "name": IDENTITY_CLIENT_SCRIPT,
        "enabled": enabled,
        "created": created,
    }


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
    from db_connector.api_identity_activation import (
        backfill_activation_item_source_pairs,
    )
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(_identity_custom_fields(), update=True)
    client_script = _install_identity_client_script()
    _add_indexes()
    migration = _migrate_recommendation_terms()
    # Reading the Single creates no business data and preserves the default-off
    # materialization switch established in the DocType schema.
    settings = frappe.get_single("CCD Identity Resolution Settings")
    activation_item_source_backfill = backfill_activation_item_source_pairs()
    return {
        "custom_fields": [
            "CCD Master-ccd_identity_resolution_tab",
            "CCD Master-ccd_identity_resolution_html",
        ],
        "client_script": client_script,
        "materialization_enabled": bool(settings.materialization_enabled),
        "recommendation_term_migration": migration,
        "activation_item_source_backfill": activation_item_source_backfill,
    }


def after_migrate() -> None:
    install_identity_resolution()
