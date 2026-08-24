"""Materialize finalized Splink and exception-component human decisions."""

from __future__ import annotations

import json
from itertools import combinations
from typing import Any, Iterable

import frappe

from db_connector.api_identity_resolution import (
    CURRENT_MEMBERSHIP_STATUSES,
    DECISION_DOCTYPE,
    GROUP_DOCTYPE,
    MEMBERSHIP_DOCTYPE,
    _append_event,
    _lock_records,
    materialization_enabled,
    materialize_identity,
    preview_materialization,
)
from db_connector.fuzzy_matching.identity import expected_identity_fingerprints

CANDIDATE_DOCTYPE = "CCD Match Review Candidate"
QUEUE_RUN_DOCTYPE = "CCD Match Review Queue Run"
COMPONENT_DOCTYPE = "CCD Match Component Review"
CANARY_DOCTYPE = "CCD Match Canary Run"
FINAL_REVIEW_STATUSES = {"Agreed", "Adjudicated"}
MAX_BULK_COMPONENT_MATERIALIZATIONS = 25
MAX_BULK_CANDIDATE_MATERIALIZATIONS = 25
REVERSAL_STATUS = "Reversed"


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _lock_named_rows(doctype: str, names: Iterable[str]) -> None:
    """Lock a bounded set of internal identity rows in deterministic order."""
    ordered = tuple(sorted({str(item) for item in names if str(item)}))
    if not ordered:
        return
    placeholders = ", ".join(["%s"] * len(ordered))
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` "
        f"WHERE name IN ({placeholders}) ORDER BY name FOR UPDATE",
        ordered,
    )


def _single_materialized_group(candidate: Any) -> str:
    try:
        values = json.loads(candidate.identity_groups_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw("The Review Candidate has invalid materialized-group history")
    groups = [str(value) for value in values if str(value)] if isinstance(values, list) else []
    if len(groups) != 1:
        frappe.throw(
            "This correction is limited to an applied Same decision with exactly one Identity Group"
        )
    return groups[0]


def _splink_same_reversal_context(candidate_name: str) -> dict[str, Any]:
    """Validate the deliberately narrow two-record Splink correction case."""
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if candidate.review_status not in FINAL_REVIEW_STATUSES:
        frappe.throw("The Review Candidate is not finalized")
    if candidate.final_label != "Same":
        frappe.throw("Only a finalized Same decision can use this correction")
    if candidate.materialization_status != "Applied":
        frappe.throw("Only an Applied Same decision can use this correction")
    if not candidate.identity_decision:
        frappe.throw("The Review Candidate has no applied Identity Decision")

    group_name = _single_materialized_group(candidate)
    decision = frappe.get_doc(DECISION_DOCTYPE, candidate.identity_decision)
    group = frappe.get_doc(GROUP_DOCTYPE, group_name)
    if decision.status != "Active" or decision.decision_type != "Same":
        frappe.throw("The original Same Identity Decision is no longer active")
    if (
        decision.origin != "Splink Human Review"
        or decision.origin_doctype != CANDIDATE_DOCTYPE
        or decision.origin_document != candidate.name
    ):
        frappe.throw("The applied Identity Decision does not belong to this Splink candidate")
    if group.status not in {"Active", "Needs Revalidation"}:
        frappe.throw("The materialized Identity Group is no longer current")
    if group.originating_decision != decision.name:
        frappe.throw(
            "This group has a different origin; use complete-component correction instead"
        )

    memberships = frappe.get_all(
        MEMBERSHIP_DOCTYPE,
        filters={
            "identity_group": group.name,
            "status": ["in", CURRENT_MEMBERSHIP_STATUSES],
        },
        fields=[
            "name",
            "ccd_master",
            "status",
            "originating_decision",
        ],
        order_by="name",
        limit_page_length=100_000,
    )
    record_ids = [str(candidate.left_record), str(candidate.right_record)]
    if len(memberships) != 2 or {str(row.ccd_master) for row in memberships} != set(record_ids):
        frappe.throw(
            "This correction is limited to a group containing exactly the candidate's two records; "
            "use complete-component correction for a larger or changed group"
        )
    if any(str(row.originating_decision) != str(decision.name) for row in memberships):
        frappe.throw("The current memberships were not both created by this Same decision")

    return {
        "candidate": candidate,
        "decision": decision,
        "group": group,
        "memberships": memberships,
        "record_ids": record_ids,
        "run": frappe.get_doc(QUEUE_RUN_DOCTYPE, candidate.queue_run),
    }


@frappe.whitelist()
def preview_reverse_splink_same(candidate_name: str) -> dict[str, Any]:
    """Preview, without writes, a two-record Applied Same -> Different correction."""
    _require_manager()
    context = _splink_same_reversal_context(candidate_name)
    candidate = context["candidate"]
    return {
        "candidate": candidate.name,
        "zero_write": True,
        "eligible": True,
        "materialization_enabled": materialization_enabled(automated=False),
        "requires_materialization_disabled": True,
        "original_identity_decision": context["decision"].name,
        "identity_group": context["group"].name,
        "memberships": [
            {
                "membership": str(row.name),
                "ccd_master": str(row.ccd_master),
                "status": str(row.status),
            }
            for row in context["memberships"]
        ],
        "planned": {
            "ended_groups": 1,
            "ended_memberships": 2,
            "new_decision_type": "Different",
            "new_exclusions": 1,
            "superseded_same_decisions": 1,
            "physical_ccd_master_merges": 0,
        },
    }


@frappe.whitelist()
def reverse_applied_splink_same(
    candidate_name: str,
    reason: str,
    confirm_candidate_name: str,
    is_demonstration: int | str = 0,
) -> dict[str, Any]:
    """Atomically correct one exact, two-member Splink Same decision to Different."""
    _require_manager()
    candidate_name = str(candidate_name or "").strip()
    reason = str(reason or "").strip()
    if not candidate_name or str(confirm_candidate_name or "").strip() != candidate_name:
        frappe.throw("Type the exact Review Candidate ID to confirm this correction")
    if not reason:
        frappe.throw("A correction reason is required")
    if materialization_enabled(automated=False):
        frappe.throw(
            "Disable Materialization before correcting an applied Same decision"
        )
    demonstration = str(is_demonstration or "0").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }

    try:
        preliminary = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
        preliminary_records = tuple(
            sorted((str(preliminary.left_record), str(preliminary.right_record)))
        )
        _lock_records(preliminary_records)
        _lock_named_rows(CANDIDATE_DOCTYPE, [candidate_name])
        candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
        locked_records = tuple(
            sorted((str(candidate.left_record), str(candidate.right_record)))
        )
        if locked_records != preliminary_records:
            frappe.throw("The Review Candidate pair changed while the correction was starting")
        if candidate.materialization_status == REVERSAL_STATUS:
            if candidate.correction_decision:
                frappe.db.rollback()
                return {
                    "status": "Already Reversed",
                    "candidate": candidate.name,
                    "correction_decision": candidate.correction_decision,
                }
            frappe.throw("The Review Candidate has an incomplete reversal record")

        context = _splink_same_reversal_context(candidate_name, lock=False)
        decision = context["decision"]
        group = context["group"]
        memberships = context["memberships"]
        record_ids = context["record_ids"]
        run = context["run"]
        _lock_named_rows(DECISION_DOCTYPE, [decision.name])
        _lock_named_rows(GROUP_DOCTYPE, [group.name])
        _lock_named_rows(MEMBERSHIP_DOCTYPE, [row.name for row in memberships])
        # Re-read after all relevant locks and re-run every eligibility check.
        context = _splink_same_reversal_context(candidate_name, lock=False)
        decision = context["decision"]
        group = context["group"]
        memberships = context["memberships"]
        record_ids = context["record_ids"]
        run = context["run"]

        now = frappe.utils.now_datetime()
        event_nonce = f"splink_same_reversal:{candidate.name}:{now}"
        for membership in memberships:
            frappe.db.set_value(
                MEMBERSHIP_DOCTYPE,
                membership.name,
                {
                    "status": "Ended",
                    "valid_to": now,
                    "ended_reason": reason,
                    "ended_by": frappe.session.user,
                },
                update_modified=False,
            )
            _append_event(
                entity_doctype=MEMBERSHIP_DOCTYPE,
                entity_name=membership.name,
                event_type="End",
                reason=reason,
                nonce=event_nonce,
                from_status=str(membership.status),
                to_status="Ended",
                identity_decision=decision.name,
                identity_group=group.name,
                identity_membership=membership.name,
                metadata={"corrected_splink_candidate": candidate.name},
                is_demonstration=demonstration,
            )
        frappe.db.set_value(
            GROUP_DOCTYPE,
            group.name,
            {
                "status": "Ended",
                "active_member_count": 0,
                "last_validation_at": now,
            },
            update_modified=False,
        )
        _append_event(
            entity_doctype=GROUP_DOCTYPE,
            entity_name=group.name,
            event_type="End",
            reason=reason,
            nonce=event_nonce,
            from_status=str(group.status),
            to_status="Ended",
            identity_decision=decision.name,
            identity_group=group.name,
            metadata={"corrected_splink_candidate": candidate.name},
            is_demonstration=demonstration,
        )

        correction = materialize_identity(
            origin="Governance Override",
            origin_doctype=CANDIDATE_DOCTYPE,
            origin_document=candidate.name,
            policy_snapshot_json=run.policy_snapshot_json,
            policy_snapshot_sha256=run.policy_snapshot_sha256,
            matching_policy=run.matching_policy,
            record_ids=record_ids,
            groups=[[record_id] for record_id in record_ids],
            exclusions=[(record_ids[0], record_ids[1])],
            reason_codes=[
                "manager_corrected_false_same",
                "human_confirmed_different",
            ],
            review_context={
                "corrected_splink_candidate": candidate.name,
                "superseded_identity_decision": decision.name,
                "ended_identity_group": group.name,
                "correction_reason": reason,
                "is_demonstration": demonstration,
            },
            governance_override=True,
            governance_notes=reason,
            is_demonstration=demonstration,
            require_enabled=False,
        )
        correction_decision = str(correction["identity_decision"])
        frappe.db.set_value(
            DECISION_DOCTYPE,
            correction_decision,
            {
                "decision_version": int(decision.decision_version or 1) + 1,
                "supersedes": decision.name,
            },
            update_modified=False,
        )
        frappe.db.set_value(
            DECISION_DOCTYPE,
            decision.name,
            {"status": "Superseded", "superseded_by": correction_decision},
            update_modified=False,
        )
        _append_event(
            entity_doctype=DECISION_DOCTYPE,
            entity_name=decision.name,
            event_type="Supersede",
            reason=reason,
            nonce=event_nonce,
            from_status="Active",
            to_status="Superseded",
            identity_decision=correction_decision,
            identity_group=group.name,
            metadata={
                "corrected_splink_candidate": candidate.name,
                "superseded_identity_decision": decision.name,
            },
            is_demonstration=demonstration,
        )
        frappe.db.set_value(
            CANDIDATE_DOCTYPE,
            candidate.name,
            {
                "materialization_status": REVERSAL_STATUS,
                "correction_decision": correction_decision,
                "reversed_at": now,
                "reversed_by": frappe.session.user,
                "reversal_reason": reason,
                "materialization_error": None,
            },
            update_modified=False,
        )
        _append_event(
            entity_doctype=CANDIDATE_DOCTYPE,
            entity_name=candidate.name,
            event_type="Supersede",
            reason=reason,
            nonce=event_nonce,
            from_status="Applied",
            to_status=REVERSAL_STATUS,
            identity_decision=correction_decision,
            identity_group=group.name,
            metadata={"superseded_identity_decision": decision.name},
            is_demonstration=demonstration,
        )
        frappe.db.commit()
        return {
            "status": REVERSAL_STATUS,
            "candidate": candidate.name,
            "superseded_identity_decision": decision.name,
            "correction_decision": correction_decision,
            "ended_identity_group": group.name,
            "ended_memberships": [str(row.name) for row in memberships],
            "created_exclusions": int(correction.get("created_exclusions") or 0),
            "is_demonstration": demonstration,
        }
    except Exception:
        frappe.db.rollback()
        raise


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


def _candidate_materialization_plan(candidate: Any) -> dict[str, Any]:
    run = frappe.get_doc(QUEUE_RUN_DOCTYPE, candidate.queue_run)
    record_ids = [str(candidate.left_record), str(candidate.right_record)]
    try:
        expected = expected_identity_fingerprints(
            (
                (record_ids[0], candidate.left_identity_fingerprint),
                (record_ids[1], candidate.right_identity_fingerprint),
            )
        )
    except ValueError as exc:
        raise frappe.ValidationError(str(exc))
    expected_modified = {
        record_ids[0]: str(candidate.left_modified_at or ""),
        record_ids[1]: str(candidate.right_modified_at or ""),
    }
    return {
        "run": run,
        "record_ids": record_ids,
        "groups": (
            [record_ids]
            if candidate.final_label == "Same"
            else [[item] for item in record_ids]
        ),
        "exclusions": (
            []
            if candidate.final_label == "Same"
            else [(record_ids[0], record_ids[1])]
        ),
        "expected_fingerprints": expected,
        "expected_modified": expected_modified,
    }


def materialize_final_candidate_if_enabled(candidate_name: str) -> dict[str, Any]:
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if candidate.review_status not in FINAL_REVIEW_STATUSES or candidate.final_label not in {"Same", "Different"}:
        return {"status": "Not Final"}
    if candidate.stale:
        _set_outcome(CANDIDATE_DOCTYPE, candidate.name, status="Stale")
        return {"status": "Stale"}
    if _pending_if_disabled(CANDIDATE_DOCTYPE, candidate.name):
        return {"status": "Pending"}
    try:
        plan = _candidate_materialization_plan(candidate)
        run = plan["run"]
        result = materialize_identity(
            origin="Splink Human Review",
            origin_doctype=CANDIDATE_DOCTYPE,
            origin_document=candidate.name,
            policy_snapshot_json=run.policy_snapshot_json,
            policy_snapshot_sha256=run.policy_snapshot_sha256,
            matching_policy=run.matching_policy,
            record_ids=plan["record_ids"],
            groups=plan["groups"],
            exclusions=plan["exclusions"],
            expected_fingerprints=plan["expected_fingerprints"] or None,
            expected_modified=plan["expected_modified"],
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


@frappe.whitelist()
def preview_review_candidate_materialization(candidate_name: str) -> dict[str, Any]:
    """Run the complete Splink human-decision safety preview without writes."""
    _require_manager()
    candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
    if (
        candidate.review_status not in FINAL_REVIEW_STATUSES
        or candidate.final_label not in {"Same", "Different"}
    ):
        frappe.throw("The Review Candidate is not finalized")
    if candidate.stale:
        frappe.throw("The Review Candidate is stale")
    plan = _candidate_materialization_plan(candidate)
    run = plan["run"]
    preview = preview_materialization(
        origin="Splink Human Review",
        origin_doctype=CANDIDATE_DOCTYPE,
        origin_document=candidate.name,
        policy_snapshot_json=run.policy_snapshot_json,
        record_ids=plan["record_ids"],
        groups=plan["groups"],
        exclusions=plan["exclusions"],
        expected_fingerprints=plan["expected_fingerprints"] or None,
        expected_modified=plan["expected_modified"],
    )
    return {
        "candidate": candidate.name,
        "review_status": candidate.review_status,
        "final_label": candidate.final_label,
        "materialization_status": candidate.materialization_status,
        "zero_write": True,
        "safe": preview["safe"],
        "conflicts": preview["conflicts"],
        "record_count": preview["record_count"],
        "group_count": preview["group_count"],
        "membership_count": preview["membership_count"],
        "exclusion_count": preview["exclusion_count"],
        "stale_record_count": preview["stale_record_count"],
        "missing_fingerprint_record_count": preview[
            "missing_fingerprint_record_count"
        ],
        "missing_modified_record_count": preview["missing_modified_record_count"],
    }


def _bulk_candidate_names(candidate_names: Any) -> list[str]:
    try:
        values = (
            json.loads(candidate_names)
            if isinstance(candidate_names, str)
            else candidate_names
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw("Selected Review Candidate names must be a JSON list")
    if not isinstance(values, (list, tuple)):
        frappe.throw("Select one or more Review Candidate rows")
    if any(not isinstance(value, str) for value in values):
        frappe.throw("Every selected Review Candidate name must be text")
    names = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not names:
        frappe.throw("Select one or more Review Candidate rows")
    if len(names) > MAX_BULK_CANDIDATE_MATERIALIZATIONS:
        frappe.throw(
            f"Select at most {MAX_BULK_CANDIDATE_MATERIALIZATIONS} Splink candidates per operation"
        )
    return names


def _preview_review_candidate(candidate_name: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate": candidate_name,
        "eligible": False,
        "review_status": "",
        "final_label": "",
        "materialization_status": "",
        "record_ids": [],
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
        candidate = frappe.get_doc(CANDIDATE_DOCTYPE, candidate_name)
        row.update(
            {
                "review_status": str(candidate.review_status or ""),
                "final_label": str(candidate.final_label or ""),
                "materialization_status": str(
                    candidate.materialization_status or ""
                ),
            }
        )
        if (
            candidate.review_status not in FINAL_REVIEW_STATUSES
            or candidate.final_label not in {"Same", "Different"}
        ):
            row["error"] = "Review Candidate is not finalized"
            return row
        if candidate.stale:
            row["error"] = "Review Candidate is stale; create a new Splink queue"
            return row
        if candidate.materialization_status not in {"Pending", "Exception"}:
            row["error"] = "Only Pending or Exception candidates can be selected"
            return row
        plan = _candidate_materialization_plan(candidate)
        run = plan["run"]
        preview = preview_materialization(
            origin="Splink Human Review",
            origin_doctype=CANDIDATE_DOCTYPE,
            origin_document=candidate.name,
            policy_snapshot_json=run.policy_snapshot_json,
            record_ids=plan["record_ids"],
            groups=plan["groups"],
            exclusions=plan["exclusions"],
            expected_fingerprints=plan["expected_fingerprints"] or None,
            expected_modified=plan["expected_modified"],
        )
        row.update(
            {
                "record_ids": list(plan["record_ids"]),
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
    except (
        frappe.ValidationError,
        frappe.PermissionError,
        frappe.DoesNotExistError,
    ) as exc:
        row["error"] = str(exc)
    return row


def _reject_overlapping_candidate_records(rows: list[dict[str, Any]]) -> None:
    owners: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for record_id in row["record_ids"]:
            owners.setdefault(str(record_id), []).append(row)
    for record_id, record_rows in owners.items():
        if len(record_rows) < 2:
            continue
        for row in record_rows:
            row["eligible"] = False
            row["safe"] = False
            row["conflicts"] = sorted(
                set(row["conflicts"]) | {"selected_candidates_overlap"}
            )
            row["error"] = (
                f"CCD record {record_id} appears in more than one selected candidate"
            )


@frappe.whitelist()
def preview_candidate_materializations(candidate_names: Any) -> dict[str, Any]:
    """Zero-write preview for an exact, bounded Splink candidate selection."""
    _require_manager()
    names = _bulk_candidate_names(candidate_names)
    rows = [_preview_review_candidate(name) for name in names]
    _reject_overlapping_candidate_records(rows)
    eligible_count = sum(bool(row["eligible"]) for row in rows)
    return {
        "selected_count": len(names),
        "eligible_count": eligible_count,
        "all_eligible": eligible_count == len(names),
        "materialization_enabled": materialization_enabled(automated=False),
        "max_candidates": MAX_BULK_CANDIDATE_MATERIALIZATIONS,
        "rows": rows,
        "totals": {
            "record_count": sum(int(row["record_count"]) for row in rows),
            "group_count": sum(int(row["group_count"]) for row in rows),
            "membership_count": sum(int(row["membership_count"]) for row in rows),
            "exclusion_count": sum(int(row["exclusion_count"]) for row in rows),
        },
    }


@frappe.whitelist()
def materialize_review_candidates(candidate_names: Any) -> dict[str, Any]:
    """Materialize one exact Splink selection as a single atomic operation."""
    _require_manager()
    names = _bulk_candidate_names(candidate_names)
    if not materialization_enabled(automated=False):
        frappe.throw(
            "Live identity materialization is disabled. Enable Materialization, then preview again."
        )
    preview = preview_candidate_materializations(names)
    invalid = [row["candidate"] for row in preview["rows"] if not row["eligible"]]
    if invalid:
        frappe.throw(
            "Every selected Splink candidate must pass preview before this atomic operation: "
            + ", ".join(invalid[:5])
        )

    results = []
    for name in names:
        result = materialize_final_candidate_if_enabled(name)
        if result.get("status") not in {"Applied", "Already Applied"}:
            frappe.throw(
                f"Splink candidate {name} was not applied; the complete selection was rolled back"
            )
        results.append(
            {
                "candidate": name,
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
    recommendation_rows = frappe.get_all(
        "CCD Match Recommendation",
        filters={
            "canary_run": review.canary_run,
            "cluster_fingerprint": review.cluster_fingerprint,
        },
        fields=[
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
            "left_identity_fingerprint",
            "right_identity_fingerprint",
        ],
        limit_page_length=100_000,
    )
    expected_modified: dict[str, str] = {}
    fingerprint_values = []
    for row in recommendation_rows:
        for record_id, modified in (
            (row.left_record, row.left_modified_at),
            (row.right_record, row.right_modified_at),
        ):
            key = str(record_id)
            value = str(modified or "")
            prior = expected_modified.setdefault(key, value)
            if prior != value:
                raise frappe.ValidationError(
                    "inconsistent_component_modified_timestamps"
                )
        fingerprint_values.extend(
            (
                (str(row.left_record), row.left_identity_fingerprint),
                (str(row.right_record), row.right_identity_fingerprint),
            )
        )
    try:
        expected = expected_identity_fingerprints(fingerprint_values)
    except ValueError as exc:
        raise frappe.ValidationError(str(exc))
    return {
        "canary": canary,
        "groups": groups,
        "record_ids": record_ids,
        "exclusions": _cross_group_exclusions(groups),
        "expected_fingerprints": expected,
        "expected_modified": expected_modified,
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
            expected_modified=plan["expected_modified"],
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
            expected_modified=plan["expected_modified"],
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
