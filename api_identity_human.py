"""Materialize finalized Splink and exception-component human decisions."""

from __future__ import annotations

import json
from itertools import combinations
from typing import Any, Iterable

import frappe

from db_connector.api_identity_resolution import (
    materialization_enabled,
    materialize_identity,
    preview_materialization,
)

CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
QUEUE_RUN_DOCTYPE = "CCD Match Review Queue Run"
COMPONENT_DOCTYPE = "CCD Match Component Review"
CANARY_DOCTYPE = "CCD Match Canary Run"
FINAL_REVIEW_STATUSES = {"Agreed", "Adjudicated"}
MAX_BULK_COMPONENT_MATERIALIZATIONS = 25


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _review_context(rows: Iterable[Any]) -> dict[str, Any]:
    return {
        "submissions": [
            {
                "reviewer": str(row.reviewer),
                "label": str(row.get("label") or row.get("decision") or ""),
                "submitted_at": str(row.submitted_at or ""),
                "is_adjudication": bool(row.is_adjudication),
            }
            for row in rows
        ]
    }


def _set_outcome(
    doctype: str,
    name: str,
    *,
    status: str,
    decision: str = "",
    groups: list[str] | None = None,
    error: str = "",
) -> None:
    frappe.db.set_value(
        doctype,
        name,
        {
            "materialization_status": status,
            "identity_decision": decision or None,
            "identity_groups_json": json.dumps(groups or []),
            "materialization_error": error[:140],
        },
        update_modified=False,
    )


def _pending_if_disabled(doctype: str, name: str) -> bool:
    # QC circuit breakers pause automatic Tiered waves, not finalized human
    # decisions.  The global materialization switch still gates both routes.
    if materialization_enabled(automated=False):
        return False
    _set_outcome(doctype, name, status="Pending")
    return True


def materialize_final_candidate_if_enabled(candidate_name: str) -> dict[str, Any]:
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if candidate.review_status not in FINAL_REVIEW_STATUSES or candidate.final_label not in {"Same", "Different"}:
        return {"status": "Not Final"}
    if candidate.stale:
        _set_outcome(CANDIDATE_DOCTYPE, candidate.name, status="Stale")
        return {"status": "Stale"}
    if _pending_if_disabled(CANDIDATE_DOCTYPE, candidate.name):
        return {"status": "Pending"}
    run = frappe.get_doc(QUEUE_RUN_DOCTYPE, candidate.queue_run)
    record_ids = [str(candidate.left_record), str(candidate.right_record)]
    expected = {
        str(candidate.left_record): str(candidate.left_identity_fingerprint),
        str(candidate.right_record): str(candidate.right_identity_fingerprint),
    }
    expected = {key: value for key, value in expected.items() if value}
    groups = [record_ids] if candidate.final_label == "Same" else [[item] for item in record_ids]
    exclusions = [] if candidate.final_label == "Same" else [(record_ids[0], record_ids[1])]
    try:
        result = materialize_identity(
            origin="Splink Human Review",
            origin_doctype=CANDIDATE_DOCTYPE,
            origin_document=candidate.name,
            policy_snapshot_json=run.policy_snapshot_json,
            policy_snapshot_sha256=run.policy_snapshot_sha256,
            matching_policy=run.matching_policy,
            record_ids=record_ids,
            groups=groups,
            exclusions=exclusions,
            expected_fingerprints=expected or None,
            reason_codes=["human_confirmed_" + candidate.final_label.casefold()],
            review_context=_review_context(candidate.review_labels),
        )
    except (frappe.ValidationError, frappe.PermissionError) as exc:
        _set_outcome(
            CANDIDATE_DOCTYPE,
            candidate.name,
            status="Exception",
            error=f"{type(exc).__name__}:{str(exc)}",
        )
        return {"status": "Exception", "error": str(exc)}
    _set_outcome(
        CANDIDATE_DOCTYPE,
        candidate.name,
        status="Applied",
        decision=result["identity_decision"],
        groups=result.get("identity_groups") or [],
    )
    return result


def _cross_group_exclusions(groups: tuple[tuple[str, ...], ...]) -> list[tuple[str, str]]:
    group_for = {
        record_id: index
        for index, group in enumerate(groups)
        for record_id in group
    }
    return [
        (left, right)
        for left, right in combinations(sorted(group_for), 2)
        if group_for[left] != group_for[right]
    ]


def _component_materialization_plan(review: Any) -> dict[str, Any]:
    groups = tuple(
        tuple(str(item) for item in group)
        for group in json.loads(review.final_groups_json or "[]")
    )
    record_ids = sorted({item for group in groups for item in group})
    if not record_ids:
        raise frappe.ValidationError("final_component_partition_missing")

    canary = frappe.get_doc(CANARY_DOCTYPE, review.canary_run)
    expected: dict[str, str] = {}
    recommendation_rows = frappe.get_all(
        "CCD Match Recommendation",
        filters={
            "canary_run": review.canary_run,
            "cluster_fingerprint": review.cluster_fingerprint,
        },
        fields=[
            "left_record",
            "right_record",
            "left_identity_fingerprint",
            "right_identity_fingerprint",
        ],
        limit_page_length=100_000,
    )
    for row in recommendation_rows:
        for record_id, fingerprint in (
            (row.left_record, row.left_identity_fingerprint),
            (row.right_record, row.right_identity_fingerprint),
        ):
            if fingerprint:
                prior = expected.setdefault(str(record_id), str(fingerprint))
                if prior != str(fingerprint):
                    raise frappe.ValidationError(
                        "inconsistent_component_identity_fingerprints"
                    )
    return {
        "canary": canary,
        "groups": groups,
        "record_ids": record_ids,
        "exclusions": _cross_group_exclusions(groups),
        "expected_fingerprints": expected,
    }


def materialize_final_component_if_enabled(review_name: str) -> dict[str, Any]:
    review = frappe.get_doc(COMPONENT_DOCTYPE, review_name)
    if review.review_status not in FINAL_REVIEW_STATUSES or not review.final_decision:
        return {"status": "Not Final"}
    if review.stale:
        _set_outcome(COMPONENT_DOCTYPE, review.name, status="Stale")
        return {"status": "Stale"}
    if _pending_if_disabled(COMPONENT_DOCTYPE, review.name):
        return {"status": "Pending"}
    try:
        plan = _component_materialization_plan(review)
        canary = plan["canary"]
        result = materialize_identity(
            origin="Component Review",
            origin_doctype=COMPONENT_DOCTYPE,
            origin_document=review.name,
            policy_snapshot_json=canary.policy_snapshot_json,
            policy_snapshot_sha256=canary.policy_snapshot_sha256,
            matching_policy=canary.matching_policy,
            record_ids=plan["record_ids"],
            groups=plan["groups"],
            exclusions=plan["exclusions"],
            expected_fingerprints=plan["expected_fingerprints"] or None,
            reason_codes=["human_component_" + review.final_decision.casefold().replace(" ", "_")],
            review_context={
                "final_decision": review.final_decision,
                **_review_context(review.review_submissions),
            },
        )
    except (frappe.ValidationError, frappe.PermissionError) as exc:
        _set_outcome(
            COMPONENT_DOCTYPE,
            review.name,
            status="Exception",
            error=f"{type(exc).__name__}:{str(exc)}",
        )
        return {"status": "Exception", "error": str(exc)}
    _set_outcome(
        COMPONENT_DOCTYPE,
        review.name,
        status="Applied",
        decision=result["identity_decision"],
        groups=result.get("identity_groups") or [],
    )
    return result


def _bulk_component_names(review_names: Any) -> list[str]:
    try:
        values = json.loads(review_names) if isinstance(review_names, str) else review_names
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw("Selected component names must be a JSON list")
    if not isinstance(values, (list, tuple)):
        frappe.throw("Select one or more Component Review rows")
    if any(not isinstance(value, str) for value in values):
        frappe.throw("Every selected Component Review name must be text")
    names = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not names:
        frappe.throw("Select one or more Component Review rows")
    if len(names) > MAX_BULK_COMPONENT_MATERIALIZATIONS:
        frappe.throw(
            f"Select at most {MAX_BULK_COMPONENT_MATERIALIZATIONS} components per operation"
        )
    return names


def _preview_component_review(review_name: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "review": review_name,
        "eligible": False,
        "review_status": "",
        "final_decision": "",
        "materialization_status": "",
        "record_count": 0,
        "group_count": 0,
        "membership_count": 0,
        "exclusion_count": 0,
        "safe": False,
        "already_applied": False,
        "conflicts": [],
        "error": "",
    }
    try:
        review = frappe.get_doc(COMPONENT_DOCTYPE, review_name)
        row.update(
            {
                "review_status": str(review.review_status or ""),
                "final_decision": str(review.final_decision or ""),
                "materialization_status": str(review.materialization_status or ""),
            }
        )
        if review.review_status not in FINAL_REVIEW_STATUSES or not review.final_decision:
            row["error"] = "Component Review is not finalized"
            return row
        if review.stale:
            row["error"] = "Component Review is stale; create a new canary"
            return row
        if review.materialization_status not in {"Pending", "Exception"}:
            row["error"] = "Only Pending or Exception components can be selected"
            return row
        plan = _component_materialization_plan(review)
        canary = plan["canary"]
        preview = preview_materialization(
            origin="Component Review",
            origin_doctype=COMPONENT_DOCTYPE,
            origin_document=review.name,
            policy_snapshot_json=canary.policy_snapshot_json,
            record_ids=plan["record_ids"],
            groups=plan["groups"],
            exclusions=plan["exclusions"],
            expected_fingerprints=plan["expected_fingerprints"] or None,
        )
        row.update(
            {
                "record_count": preview["record_count"],
                "group_count": preview["group_count"],
                "membership_count": preview["membership_count"],
                "exclusion_count": preview["exclusion_count"],
                "safe": bool(preview["safe"]),
                "already_applied": bool(preview["already_applied"]),
                "conflicts": list(preview["conflicts"]),
                "eligible": bool(preview["safe"]),
            }
        )
        if not preview["safe"]:
            row["error"] = "Identity safety checks failed"
    except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError) as exc:
        row["error"] = str(exc)
    return row


@frappe.whitelist()
def preview_component_materializations(review_names: Any) -> dict[str, Any]:
    """Zero-write preview for the exact Component Review rows selected by a manager."""
    _require_manager()
    names = _bulk_component_names(review_names)
    rows = [_preview_component_review(name) for name in names]
    eligible_count = sum(bool(row["eligible"]) for row in rows)
    return {
        "selected_count": len(names),
        "eligible_count": eligible_count,
        "all_eligible": eligible_count == len(names),
        "materialization_enabled": materialization_enabled(automated=False),
        "max_components": MAX_BULK_COMPONENT_MATERIALIZATIONS,
        "rows": rows,
        "totals": {
            "record_count": sum(int(row["record_count"]) for row in rows),
            "group_count": sum(int(row["group_count"]) for row in rows),
            "membership_count": sum(int(row["membership_count"]) for row in rows),
            "exclusion_count": sum(int(row["exclusion_count"]) for row in rows),
        },
    }


@frappe.whitelist()
def materialize_component_reviews(review_names: Any) -> dict[str, Any]:
    """Materialize one explicit, preflighted selection as an atomic operation."""
    _require_manager()
    names = _bulk_component_names(review_names)
    if not materialization_enabled(automated=False):
        frappe.throw(
            "Live identity materialization is disabled. Enable Materialization, then preview again."
        )
    preview = preview_component_materializations(names)
    invalid = [row["review"] for row in preview["rows"] if not row["eligible"]]
    if invalid:
        frappe.throw(
            "Every selected component must pass preview before this atomic operation: "
            + ", ".join(invalid[:5])
        )

    results = []
    for name in names:
        result = materialize_final_component_if_enabled(name)
        if result.get("status") not in {"Applied", "Already Applied"}:
            frappe.throw(
                f"Component {name} was not applied; the complete selection was rolled back"
            )
        results.append(
            {
                "review": name,
                "status": result["status"],
                "identity_decision": result.get("identity_decision") or "",
                "created_groups": int(result.get("created_groups") or 0),
                "created_memberships": int(result.get("created_memberships") or 0),
                "created_exclusions": int(result.get("created_exclusions") or 0),
            }
        )
    frappe.db.commit()
    return {
        "status": "Applied",
        "selected_count": len(names),
        "results": results,
        "created_groups": sum(row["created_groups"] for row in results),
        "created_memberships": sum(row["created_memberships"] for row in results),
        "created_exclusions": sum(row["created_exclusions"] for row in results),
    }


@frappe.whitelist()
def materialize_review_candidate(candidate_name: str) -> dict[str, Any]:
    _require_manager()
    result = materialize_final_candidate_if_enabled(candidate_name)
    frappe.db.commit()
    return result


@frappe.whitelist()
def materialize_component_review(review_name: str) -> dict[str, Any]:
    _require_manager()
    result = materialize_final_component_if_enabled(review_name)
    frappe.db.commit()
    return result
