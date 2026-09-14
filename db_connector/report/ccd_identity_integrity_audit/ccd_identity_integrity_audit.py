"""System Manager report for active and historical identity-source orphans."""

from __future__ import annotations

from typing import Any

from frappe import _

from db_connector.api_identity_retirement import get_orphan_integrity_audit


def execute(filters: dict[str, Any] | None = None):
    result = get_orphan_integrity_audit()
    active = result["active_issue_counts"]
    historical = result["historical_affected_counts"]
    categories = sorted(set(active) | set(historical))
    rows = [
        {
            "category": category.replace("_", " ").title(),
            "active_issue_count": int(active.get(category, 0)),
            "historical_affected_count": int(historical.get(category, 0)),
        }
        for category in categories
    ]
    columns = [
        {"fieldname": "category", "label": _("Integrity Category"), "fieldtype": "Data", "width": 280},
        {"fieldname": "active_issue_count", "label": _("Active Issues"), "fieldtype": "Int", "width": 140},
        {"fieldname": "historical_affected_count", "label": _("Historical Rows Affected"), "fieldtype": "Int", "width": 190},
    ]
    summary = [
        {
            "value": result["active_issue_count"],
            "label": _("Active integrity issues"),
            "datatype": "Int",
            "indicator": "Red" if result["active_issue_count"] else "Green",
        },
        {
            "value": result["missing_ccd_master_count"],
            "label": _("Retired CCD Master IDs observed"),
            "datatype": "Int",
        },
        {
            "value": result["planned_write_count"],
            "label": _("Lifecycle writes needed"),
            "datatype": "Int",
        },
    ]
    message = _("Read-only audit. Scope fingerprint: {0}").format(
        result["scope_fingerprint"]
    )
    return columns, rows, message, None, summary
