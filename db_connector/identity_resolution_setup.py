"""Idempotent schema/UI setup for the CCD Identity Resolution workflow."""

from __future__ import annotations

import frappe


IDENTITY_CLIENT_SCRIPT = "CCD Master Identity Resolution"
IDENTITY_LIST_CLIENT_SCRIPT = "CCD Master Identity Resolution List"
REGISTRATION_CANCEL_CLIENT_SCRIPT = "CCD Registration Governed Cancellation"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
UNIFIED_PERSON_SETTINGS_DOCTYPE = "CCD Unified Person Settings"
UNIFIED_PERSON_REGISTER_REPORT = "CCD Unified Person Register"


def _initialize_unified_person_sequence() -> dict[str, int]:
    """Create the fail-closed non-recycled sequence state after schema sync."""
    if not frappe.db.exists("DocType", UNIFIED_PERSON_SETTINGS_DOCTYPE):
        return {"initialized": 0, "last_sequence": 0}
    value = frappe.db.get_single_value(
        UNIFIED_PERSON_SETTINGS_DOCTYPE, "last_sequence"
    )
    initialized = int(value in (None, ""))
    if initialized:
        frappe.db.set_single_value(
            UNIFIED_PERSON_SETTINGS_DOCTYPE, "last_sequence", 0
        )
    return {
        "initialized": initialized,
        "last_sequence": int(
            frappe.db.get_single_value(
                UNIFIED_PERSON_SETTINGS_DOCTYPE, "last_sequence"
            )
            or 0
        ),
    }


def _backfill_fail_closed_automation_defaults() -> dict[str, int]:
    """Initialize newly added controls without changing an existing decision."""
    defaults = {
        "automatic_tiered_enabled": 0,
        "automatic_qc_assignment_enabled": 0,
        "automation_paused": 0,
        "automation_control_revision": 0,
        "automatic_tiered_components_per_run": 10,
        "automatic_tiered_schedule": "Daily",
        "qc_assignment_interval_days": 7,
    }
    initialized = 0
    for fieldname, value in defaults.items():
        if frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname) in (None, ""):
            frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)
            initialized += 1
    return {"initialized": initialized}


def _upsert_identity_client_script(
    *,
    name: str,
    view: str,
    public_filename: str,
    enabled: bool,
    dt: str = "CCD Master",
) -> dict[str, object]:
    script = frappe.read_file(
        frappe.get_app_path("db_connector", "public", "js", public_filename)
    )
    values = {
        "dt": dt,
        "view": view,
        "enabled": enabled,
        "script": script,
    }
    created = not frappe.db.exists("Client Script", name)
    if created:
        frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": name,
                **values,
            }
        ).insert(ignore_permissions=True)
    else:
        client_script = frappe.get_doc("Client Script", name)
        changed = any(
            client_script.get(fieldname) != value
            for fieldname, value in values.items()
        )
        if changed:
            client_script.update(values)
            client_script.save(ignore_permissions=True)
    return {"name": name, "view": view, "enabled": enabled, "created": created}


def _install_identity_client_scripts() -> dict[str, dict[str, object]]:
    """Load CCD Master form/list integrations through the custom-DocType path."""
    # FormMeta.add_code() intentionally skips doctype_js hooks for custom
    # DocTypes, and the same boundary applies to list scripts. CCD Master is
    # custom on this site, so enabled Client Scripts are required for both the
    # form renderer and the list entry point. Keep them disabled on a future
    # standard DocType, where the normal hooks provide the same code.
    enabled = bool(frappe.db.get_value("DocType", "CCD Master", "custom"))
    installed = {
        "form": _upsert_identity_client_script(
            name=IDENTITY_CLIENT_SCRIPT,
            view="Form",
            public_filename="ccd_master_identity_resolution.js",
            enabled=enabled,
        ),
        "list": _upsert_identity_client_script(
            name=IDENTITY_LIST_CLIENT_SCRIPT,
            view="List",
            public_filename="ccd_master_identity_resolution_list.js",
            enabled=enabled,
        ),
        "registration_cancel": _upsert_identity_client_script(
            name=REGISTRATION_CANCEL_CLIENT_SCRIPT,
            view="Form",
            public_filename="ccd_registration_governed_cancel.js",
            enabled=bool(
                frappe.db.get_value("DocType", "CCD Registration", "custom")
            ),
            dt="CCD Registration",
        ),
    }
    frappe.clear_cache(doctype="CCD Master")
    return installed


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
        ],
        "CCD Registration": [
            {
                "fieldname": "ccd_stable_source_key",
                "fieldtype": "Data",
                "label": "Stable CCD Source Key",
                "insert_after": "ccd_reg_doctype",
                "read_only": 1,
                "allow_on_submit": 1,
                "no_copy": 1,
            },
        ],
    }


def _backfill_registration_source_keys() -> dict[str, object]:
    from db_connector.api_identity_retirement import stable_source_key

    if not frappe.get_meta("CCD Registration").has_field("ccd_stable_source_key"):
        return {"updated": 0, "active_conflicts": {}}
    updated = 0
    active_by_source: dict[str, list[str]] = {}
    registrations = frappe.get_all(
        "CCD Registration",
        fields=["name", "ccd_reg_doctype", "ccd_stable_source_key", "docstatus"],
        limit_page_length=100_000,
    )
    for row in registrations:
        if not row.ccd_reg_doctype:
            continue
        source = stable_source_key(row.ccd_reg_doctype)
        if str(row.ccd_stable_source_key or "") != source:
            frappe.db.set_value(
                "CCD Registration",
                row.name,
                "ccd_stable_source_key",
                source,
                update_modified=False,
            )
            updated += 1
        if int(row.docstatus or 0) == 1:
            active_by_source.setdefault(source, []).append(str(row.name))
    conflicts = {
        source: sorted(names)
        for source, names in active_by_source.items()
        if len(names) > 1
    }
    return {"updated": updated, "active_conflicts": conflicts}


def _disable_legacy_registration_cancel_script() -> dict[str, object]:
    name = "CCD Registration Before Cancel"
    if not frappe.db.exists("Server Script", name):
        return {"name": name, "found": False, "disabled": False}
    was_disabled = bool(frappe.db.get_value("Server Script", name, "disabled"))
    if not was_disabled:
        frappe.db.set_value(
            "Server Script", name, "disabled", 1, update_modified=False
        )
    return {"name": name, "found": True, "disabled": True}


def _add_indexes() -> None:
    indexes = (
        ("CCD Identity Membership", ["ccd_master", "status"], "ccd_identity_member_current"),
        ("CCD Identity Membership", ["identity_group", "status"], "ccd_identity_group_current"),
        ("CCD Identity Exclusion", ["left_record", "right_record", "status"], "ccd_identity_exclusion_pair"),
        ("CCD Identity Event", ["entity_doctype", "entity_name", "event_at"], "ccd_identity_event_entity"),
        ("CCD Match Recommendation", ["left_record", "rollout_state"], "ccd_recommendation_left_lifecycle"),
        ("CCD Match Recommendation", ["right_record", "rollout_state"], "ccd_recommendation_right_lifecycle"),
        ("CCD Match Evaluation Pair", ["left_record", "stale"], "ccd_evaluation_left_lifecycle"),
        ("CCD Match Evaluation Pair", ["right_record", "stale"], "ccd_evaluation_right_lifecycle"),
        ("CCD Match Review Candidate", ["left_record", "stale"], "ccd_candidate_left_lifecycle"),
        ("CCD Match Review Candidate", ["right_record", "stale"], "ccd_candidate_right_lifecycle"),
        ("CCD Master", ["ccd_reg_source", "ccd_source_key"], "ccd_master_source_lifecycle"),
        ("CCD Unified Person Membership", ["ccd_master", "status"], "ccd_unified_member_current"),
        ("CCD Unified Person Membership", ["unified_person", "status"], "ccd_unified_person_current"),
        ("CCD Unified Person Membership", ["source_lineage_key", "status"], "ccd_unified_source_lineage"),
        ("CCD Unified Person Membership", ["status", "valid_from", "name"], "ccd_unified_membership_report"),
        ("CCD Unified Person Alias", ["alias_person", "status"], "ccd_unified_alias_current"),
        ("CCD Unified Person Alias", ["canonical_person", "status"], "ccd_unified_canonical_current"),
    )
    for doctype, fields, index_name in indexes:
        if frappe.db.table_exists(doctype):
            frappe.db.add_index(doctype, fields, index_name=index_name)


def _configure_unified_person_register() -> dict[str, object]:
    """Keep the indexed operational register interactive after migration."""
    if not frappe.db.exists("Report", UNIFIED_PERSON_REGISTER_REPORT):
        return {"found": False, "prepared_report": False, "updated": False}
    was_prepared = bool(
        frappe.db.get_value(
            "Report", UNIFIED_PERSON_REGISTER_REPORT, "prepared_report"
        )
    )
    if was_prepared:
        frappe.db.set_value(
            "Report",
            UNIFIED_PERSON_REGISTER_REPORT,
            "prepared_report",
            0,
            update_modified=False,
        )
    return {
        "found": True,
        "prepared_report": False,
        "updated": was_prepared,
    }


def _migrate_recommendation_terms() -> dict[str, int]:
    if not frappe.db.table_exists("CCD Match Recommendation"):
        return {
            "active_to_approved": 0,
            "reversed_to_withdrawn": 0,
            "qc_finalized_at_backfilled": 0,
        }
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
    qc_finalized_at_backfilled = 0
    if frappe.get_meta("CCD Match Recommendation").has_field("qc_finalized_at"):
        qc_finalized_at_backfilled = frappe.db.count(
            "CCD Match Recommendation",
            {
                "qc_review_status": ["in", ["Agreed", "Adjudicated"]],
                "qc_finalized_at": ["is", "not set"],
            },
        )
        if qc_finalized_at_backfilled:
            frappe.db.sql(
                "UPDATE `tabCCD Match Recommendation` "
                "SET qc_finalized_at = modified "
                "WHERE qc_review_status IN ('Agreed', 'Adjudicated') "
                "AND qc_finalized_at IS NULL"
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
        "qc_finalized_at_backfilled": int(qc_finalized_at_backfilled),
    }


def install_identity_resolution() -> dict[str, object]:
    from db_connector.api_identity_activation import (
        backfill_activation_item_source_pairs,
    )
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(_identity_custom_fields(), update=True)
    client_scripts = _install_identity_client_scripts()
    _add_indexes()
    unified_person_register = _configure_unified_person_register()
    migration = _migrate_recommendation_terms()
    automation_defaults = _backfill_fail_closed_automation_defaults()
    registration_sources = _backfill_registration_source_keys()
    legacy_cancel_script = _disable_legacy_registration_cancel_script()
    unified_person_sequence = _initialize_unified_person_sequence()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    activation_item_source_backfill = backfill_activation_item_source_pairs()
    return {
        "custom_fields": [
            "CCD Master-ccd_identity_resolution_tab",
            "CCD Master-ccd_identity_resolution_html",
        ],
        # Keep the original singular key for callers written before the List
        # Client Script was introduced.
        "client_script": client_scripts["form"],
        "client_scripts": client_scripts,
        "materialization_enabled": bool(settings.materialization_enabled),
        "automatic_tiered_enabled": bool(settings.automatic_tiered_enabled),
        "automatic_qc_assignment_enabled": bool(
            settings.automatic_qc_assignment_enabled
        ),
        "automation_defaults": automation_defaults,
        "registration_sources": registration_sources,
        "legacy_cancel_script": legacy_cancel_script,
        "recommendation_term_migration": migration,
        "activation_item_source_backfill": activation_item_source_backfill,
        "unified_person_sequence": unified_person_sequence,
        "unified_person_register": unified_person_register,
    }


def after_migrate() -> None:
    install_identity_resolution()
