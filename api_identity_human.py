"""Materialize finalized Splink and exception-component human decisions."""

from __future__ import annotations

import json
from itertools import combinations
from typing import Any, Iterable

import frappe

from db_connector.api_identity_resolution import (
    materialization_enabled,
    materialize_identity,
)

CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
QUEUE_RUN_DOCTYPE = "CCD Match Review Queue Run"
COMPONENT_DOCTYPE = "CCD Match Component Review"
CANARY_DOCTYPE = "CCD Match Canary Run"
FINAL_REVIEW_STATUSES = {"Agreed", "Adjudicated"}


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


def materialize_final_component_if_enabled(review_name: str) -> dict[str, Any]:
    review = frappe.get_doc(COMPONENT_DOCTYPE, review_name)
    if review.review_status not in FINAL_REVIEW_STATUSES or not review.final_decision:
        return {"status": "Not Final"}
    if review.stale:
        _set_outcome(COMPONENT_DOCTYPE, review.name, status="Stale")
        return {"status": "Stale"}
    if _pending_if_disabled(COMPONENT_DOCTYPE, review.name):
        return {"status": "Pending"}
    groups = tuple(
        tuple(str(item) for item in group)
        for group in json.loads(review.final_groups_json or "[]")
    )
    record_ids = sorted({item for group in groups for item in group})
    if not record_ids:
        _set_outcome(
            COMPONENT_DOCTYPE,
            review.name,
            status="Exception",
            error="final_component_partition_missing",
        )
        return {"status": "Exception"}
    canary = frappe.get_doc(CANARY_DOCTYPE, review.canary_run)
    exclusions = _cross_group_exclusions(groups)
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
    expected: dict[str, str] = {}
    for row in recommendation_rows:
        for record_id, fingerprint in (
            (row.left_record, row.left_identity_fingerprint),
            (row.right_record, row.right_identity_fingerprint),
        ):
            if fingerprint:
                prior = expected.setdefault(str(record_id), str(fingerprint))
                if prior != str(fingerprint):
                    _set_outcome(
                        COMPONENT_DOCTYPE,
                        review.name,
                        status="Exception",
                        error="inconsistent_component_identity_fingerprints",
                    )
                    return {"status": "Exception"}
    try:
        result = materialize_identity(
            origin="Component Review",
            origin_doctype=COMPONENT_DOCTYPE,
            origin_document=review.name,
            policy_snapshot_json=canary.policy_snapshot_json,
            policy_snapshot_sha256=canary.policy_snapshot_sha256,
            matching_policy=canary.matching_policy,
            record_ids=record_ids,
            groups=groups,
            exclusions=exclusions,
            expected_fingerprints=expected or None,
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
