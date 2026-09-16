"""System Manager report for active and historical identity-source orphans."""

from __future__ import annotations

from typing import Any

from frappe import _

from db_connector.api_identity_retirement import get_orphan_integrity_audit
from db_connector.api_unified_person import get_unified_person_integrity_audit


def execute(filters: dict[str, Any] | None = None):
    result = get_orphan_integrity_audit()
    unified = get_unified_person_integrity_audit()
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
    rows.extend(
        {
            "category": _("Unified Person: {0}").format(
                category.replace("_", " ").title()
            ),
            "active_issue_count": int(count),
            "historical_affected_count": 0,
        }
        for category, count in sorted(
            (unified.get("active_issue_counts") or {}).items()
        )
    )
    total_active = int(result["active_issue_count"]) + int(
        unified.get("active_issue_count") or 0
    )
    columns = [
        {"fieldname": "category", "label": _("Integrity Category"), "fieldtype": "Data", "width": 280},
        {"fieldname": "active_issue_count", "label": _("Active Issues"), "fieldtype": "Int", "width": 140},
        {"fieldname": "historical_affected_count", "label": _("Historical Rows Affected"), "fieldtype": "Int", "width": 190},
    ]
    summary = [
        {
            "value": total_active,
            "label": _("Active integrity issues"),
            "datatype": "Int",
            "indicator": "Red" if total_active else "Green",
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
        {
            "value": unified.get("unified_person_count") or 0,
            "label": _("Issued Unified People"),
            "datatype": "Int",
        },
        {
            "value": unified.get("active_membership_count") or 0,
            "label": _("Active Unified Memberships"),
            "datatype": "Int",
        },
    ]
    message = _("Read-only audit. Scope fingerprint: {0}").format(
        result["scope_fingerprint"]
    )
    return columns, rows, message, None, summary
