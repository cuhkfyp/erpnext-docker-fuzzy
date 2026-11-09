"""Governed Tiered one-to-many prospective validation and bounded automation."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter, defaultdict
from typing import Any, Iterable

import frappe
from frappe.utils import add_days, get_datetime, now_datetime

from db_connector.api_fuzzy_canary import (
    FINAL_REVIEW_STATUSES,
    REVIEW_ROLE,
    SENSITIVE_ROLE,
    _component_stale,
)
from db_connector.api_identity_resolution import (
    _append_event,
    materialization_enabled,
    materialize_identity,
)
from db_connector.fuzzy_matching.identity import expected_identity_fingerprints
from db_connector.fuzzy_matching.metrics import wilson_interval
from db_connector.fuzzy_matching.tiered_one_to_many_validation import (
    HOLDOUT_SIZE,
    MINIMUM_ELIGIBLE_HOLDOUT,
    VALIDATION_SIZE,
    component_precision_gate,
    component_stratum,
    deterministic_qc_sample,
    holdout_and_combined_gate,
    ongoing_qc_plan,
    select_component_cohorts,
    validate_seed,
)


VALIDATION_DOCTYPE = "CCD Tiered One-to-Many Validation"
BATCH_DOCTYPE = "CCD Tiered One-to-Many Batch"
COMPONENT_DOCTYPE = "CCD Match Component Review"
RECOMMENDATION_DOCTYPE = "CCD Match Recommendation"
CANARY_DOCTYPE = "CCD Match Canary Run"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
INVESTIGATION_DOCTYPE = "CCD Identity QC Investigation"
IDENTITY_EVENT_DOCTYPE = "CCD Identity Event"
ONE_TO_MANY_REASON = "one_to_many_source_conflict"
QC_CAPACITY_GATE_CONTROL = "one_to_many_qc_capacity_gate"
OPEN_REVIEW_STATUSES = {
    "Unreviewed",
    "Partially Reviewed",
    "Positive Confirmation Required",
    "Needs Adjudication",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _set_settings(values: dict[str, Any]) -> None:
    for fieldname, value in values.items():
        frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _require_reviewer() -> None:
    roles = set(frappe.get_roles())
    if not ({"System Manager", REVIEW_ROLE, SENSITIVE_ROLE} & roles):
        frappe.throw("CCD Match Reviewer role is required", frappe.PermissionError)


def _selected_canary(run_name: str | None = None) -> Any:
    selected = str(run_name or "").strip()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if not selected and settings.automatic_tiered_policy:
        rows = frappe.get_all(
            CANARY_DOCTYPE,
            filters={
                "matching_policy": settings.automatic_tiered_policy,
                "status": ["in", ["Ready", "Active"]],
            },
            fields=["name"],
            order_by="creation desc",
            limit=1,
        )
        if rows:
            selected = str(rows[0].name)
    if not selected:
        selected = str(settings.automatic_tiered_canary or "")
    if not selected:
        frappe.throw("No Ready or Active Tiered Canary is available")
    run = frappe.get_doc(CANARY_DOCTYPE, selected)
    if run.status not in {"Ready", "Active"}:
        frappe.throw("One-to-Many validation requires a Ready or Active Canary")
    return run


def _require_frozen_provenance(validation: Any) -> Any:
    """Fail the complete authorization closed if its frozen policy moved."""
    run = frappe.get_doc(CANARY_DOCTYPE, validation.canary_run)
    mismatches = []
    if run.status not in {"Ready", "Active"}:
        mismatches.append(f"canary_status:{run.status}")
    for fieldname in (
        "matching_policy",
        "policy_version",
        "policy_snapshot_sha256",
    ):
        if str(getattr(run, fieldname, "") or "") != str(
            getattr(validation, fieldname, "") or ""
        ):
            mismatches.append(f"changed_{fieldname}")
    if not mismatches:
        return run
    reason = "frozen_provenance_changed:" + ",".join(mismatches)
    validation.db_set(
        {"status": "Stale", "error_summary": reason}, update_modified=False
    )
    _set_settings(
        {
            "automatic_tiered_one_to_many_enabled": 0,
            "tiered_one_to_many_automation_paused": 1,
            "tiered_one_to_many_pause_reason": reason,
        }
    )
    frappe.db.commit()
    frappe.throw(
        "The complete Tiered one-to-many authorization is stale because its "
        f"frozen provenance changed: {', '.join(mismatches)}"
    )


def _component_inventory(run_name: str, maximum: int) -> dict[str, Any]:
    rows = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name, "status": "Exception"},
        fields=[
            "name",
            "cluster_fingerprint",
            "cluster_size",
            "left_record",
            "right_record",
            "left_source",
            "right_source",
            "safety_reasons_json",
            "component_review",
        ],
        order_by="cluster_fingerprint, name",
        limit_page_length=100_000,
    )
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        try:
            safety = {str(item) for item in json.loads(row.safety_reasons_json or "[]")}
        except (TypeError, ValueError):
            safety = set()
        if ONE_TO_MANY_REASON in safety:
            row._safety = safety
            grouped[str(row.cluster_fingerprint)].append(row)

    review_names = sorted(
        {
            str(row.component_review)
            for members in grouped.values()
            for row in members
            if row.component_review
        }
    )
    reviews = {
        str(row.name): row
        for row in frappe.get_all(
            COMPONENT_DOCTYPE,
            filters={"name": ["in", review_names]},
            fields=[
                "name",
                "cluster_fingerprint",
                "cluster_size",
                "review_status",
                "stale",
                "materialization_status",
                "tiered_one_to_many_validation",
            ],
            limit_page_length=max(len(review_names), 1),
        )
    } if review_names else {}

    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    all_strata: Counter[str] = Counter()
    for fingerprint in sorted(grouped):
        members = grouped[fingerprint]
        record_sources: dict[str, str] = {}
        safety: set[str] = set()
        review_names_for_component: set[str] = set()
        declared_size = 0
        for row in members:
            declared_size = max(declared_size, int(row.cluster_size or 0))
            record_sources[str(row.left_record)] = str(row.left_source or "")
            record_sources[str(row.right_record)] = str(row.right_source or "")
            safety.update(row._safety)
            if row.component_review:
                review_names_for_component.add(str(row.component_review))
        component_size = max(declared_size, len(record_sources))
        if component_size > int(maximum):
            continue
        source_counts = Counter(record_sources.values())
        source_pattern = "|".join(
            f"{source}:{count}" for source, count in sorted(source_counts.items())
        )
        all_strata[f"size={component_size}|sources={source_pattern}"] += 1
        reasons = sorted(reason for reason in safety if reason != ONE_TO_MANY_REASON)
        if len(review_names_for_component) != 1:
            reasons.append("component_review_missing_or_ambiguous")
            review_name = ""
        else:
            review_name = next(iter(review_names_for_component))
        review = reviews.get(review_name)
        if not review:
            reasons.append("component_review_missing")
        else:
            if review.stale:
                reasons.append("component_review_stale")
            if str(review.materialization_status or "") in {
                "Applied",
                "Corrected",
                "Superseded",
            }:
                reasons.append("already_materialized")
            if str(review.tiered_one_to_many_validation or ""):
                reasons.append("already_reserved")
            if str(review.review_status or "") != "Unreviewed":
                reasons.append("previously_reviewed")
        item = {
            "name": review_name,
            "cluster_fingerprint": fingerprint,
            "component_size": component_size,
            "source_pattern": source_pattern,
            "recommendation_names": [str(row.name) for row in members],
            "safety_reasons": sorted(safety),
        }
        if reasons:
            excluded.append({**item, "ineligibility_reasons": sorted(set(reasons))})
        else:
            eligible.append(item)
    return {
        "eligible": eligible,
        "excluded": excluded,
        "population_allocation": dict(sorted(all_strata.items())),
    }


@frappe.whitelist(methods=["POST"])
def create_validation_proposal(
    run_name: str | None = None,
    maximum_component_size: int | str | None = None,
) -> dict[str, Any]:
    _require_manager()
    run = _selected_canary(run_name)
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    maximum = int(
        maximum_component_size
        if maximum_component_size not in (None, "")
        else settings.maximum_tiered_one_to_many_component_size or 3
    )
    if maximum < 3 or maximum > 100:
        frappe.throw("Maximum component size must be between 3 and 100")
    existing = frappe.db.get_value(
        VALIDATION_DOCTYPE,
        {
            "canary_run": run.name,
            "maximum_component_size": maximum,
            "status": ["not in", ["Rejected", "Failed", "Stale"]],
        },
        "name",
    )
    if existing:
        return {"validation": str(existing), "existing": True}
    inventory = _component_inventory(run.name, maximum)
    if len(inventory["eligible"]) < VALIDATION_SIZE + HOLDOUT_SIZE:
        frappe.throw(
            f"Only {len(inventory['eligible'])} eligible components are available; "
            f"{VALIDATION_SIZE + HOLDOUT_SIZE} are required"
        )
    seed = secrets.token_hex(32)
    validate_seed(seed)
    manifest = [
        {
            "name": row["name"],
            "cluster_fingerprint": row["cluster_fingerprint"],
            "component_size": row["component_size"],
            "source_pattern": row["source_pattern"],
        }
        for row in inventory["eligible"]
    ]
    doc = frappe.get_doc(
        {
            "doctype": VALIDATION_DOCTYPE,
            "canary_run": run.name,
            "matching_policy": run.matching_policy,
            "policy_version": run.policy_version,
            "policy_snapshot_sha256": run.policy_snapshot_sha256,
            "maximum_component_size": maximum,
            "validation_seed": seed,
            "status": "Prepared",
            "development_only": 1,
            "eligible_component_count": len(inventory["eligible"]),
            "excluded_component_count": len(inventory["excluded"]),
            "eligible_manifest_sha256": _sha(manifest),
            "population_allocation_json": _json(inventory["population_allocation"]),
            "validation_target_count": VALIDATION_SIZE,
            "holdout_target_count": HOLDOUT_SIZE,
            "first_approval_status": "Not Requested",
            "holdout_status": "Not Created",
            "second_approval_status": "Not Requested",
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "validation": doc.name,
        "existing": False,
        "eligible_component_count": len(inventory["eligible"]),
        "excluded_component_count": len(inventory["excluded"]),
    }


@frappe.whitelist(methods=["POST"])
def freeze_validation_cohorts(validation_name: str) -> dict[str, Any]:
    _require_manager()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    if validation.status == "Reviewing":
        return {"validation": validation.name, "status": validation.status}
    if validation.status != "Prepared":
        frappe.throw("Only a prepared One-to-Many validation can be frozen")
    _require_frozen_provenance(validation)
    inventory = _component_inventory(
        str(validation.canary_run), int(validation.maximum_component_size)
    )
    manifest = [
        {
            "name": row["name"],
            "cluster_fingerprint": row["cluster_fingerprint"],
            "component_size": row["component_size"],
            "source_pattern": row["source_pattern"],
        }
        for row in inventory["eligible"]
    ]
    if _sha(manifest) != str(validation.eligible_manifest_sha256):
        frappe.throw(
            "The eligible one-to-many population changed after proposal preparation; create a new proposal"
        )
    cohorts = select_component_cohorts(
        inventory["eligible"], seed=str(validation.validation_seed)
    )
    for cohort, names, status in (
        ("Validation", cohorts["validation"], "Reserved Validation"),
        ("Blinded Holdout", cohorts["holdout"], "Reserved Holdout"),
    ):
        for order, name in enumerate(names, start=1):
            frappe.db.set_value(
                COMPONENT_DOCTYPE,
                name,
                {
                    "tiered_one_to_many_validation": validation.name,
                    "tiered_governance_cohort": cohort,
                    "tiered_automation_status": status,
                    "tiered_sample_order": order,
                },
                update_modified=False,
            )
    validation.db_set(
        {
            "status": "Reviewing",
            "validation_allocation_json": _json(
                cohorts["validation_allocation"]
            ),
            "holdout_allocation_json": _json(cohorts["holdout_allocation"]),
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "validation": validation.name,
        "status": "Reviewing",
        "validation_count": len(cohorts["validation"]),
        "holdout_count": len(cohorts["holdout"]),
    }


def _pending_reviews_for_reviewer(
    validation_name: str, cohort: str
) -> tuple[Any, list[Any]]:
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    allowed_statuses = {
        "Validation": {"Reviewing"},
        "Blinded Holdout": {"Holdout Reviewing"},
        "Ongoing QC": {"Authorized"},
    }
    if cohort not in allowed_statuses:
        frappe.throw("This governed cohort does not support reviewer navigation")
    if validation.status not in allowed_statuses[cohort]:
        frappe.throw(f"The {cohort} cohort is not open for review")
    filters: dict[str, Any] = {
        "tiered_one_to_many_validation": validation.name,
        "tiered_governance_cohort": cohort,
        "stale": 0,
        "review_status": ["in", sorted(OPEN_REVIEW_STATUSES)],
    }
    if cohort in {"Blinded Holdout", "Ongoing QC"}:
        filters["tiered_automation_status"] = ["in", ["Applied", "QC Selected"]]
    candidates = frappe.get_all(
        COMPONENT_DOCTYPE,
        filters=filters,
        fields=["name", "tiered_sample_order"],
        order_by="tiered_sample_order, name",
        limit_page_length=100_000,
    )
    if not candidates:
        return validation, []
    submitted = set(
        frappe.get_all(
            "CCD Match Component Review Submission",
            filters={
                "parent": ["in", [row.name for row in candidates]],
                "parenttype": COMPONENT_DOCTYPE,
                "parentfield": "review_submissions",
                "reviewer": frappe.session.user,
                "is_adjudication": 0,
            },
            pluck="parent",
            limit_page_length=100_000,
        )
    )
    return validation, [row for row in candidates if row.name not in submitted]


def _next_review(validation_name: str, cohort: str) -> dict[str, str]:
    _require_reviewer()
    _validation, candidates = _pending_reviews_for_reviewer(validation_name, cohort)
    if candidates:
        return {"review": str(candidates[0].name), "cohort": cohort}
    return {"review": "", "cohort": cohort}


@frappe.whitelist()
def get_next_validation_component(validation_name: str) -> dict[str, str]:
    return _next_review(validation_name, "Validation")


@frappe.whitelist()
def get_next_holdout_component(validation_name: str) -> dict[str, str]:
    return _next_review(validation_name, "Blinded Holdout")


@frappe.whitelist()
def get_next_ongoing_qc_component(validation_name: str) -> dict[str, str]:
    return _next_review(validation_name, "Ongoing QC")


@frappe.whitelist()
def get_adjacent_governed_component(
    review_name: str, direction: str = "next"
) -> dict[str, str]:
    """Navigate only within the current frozen cohort and reviewer workload."""
    _require_reviewer()
    review = frappe.get_doc(COMPONENT_DOCTYPE, review_name)
    validation_name = str(review.tiered_one_to_many_validation or "")
    cohort = str(review.tiered_governance_cohort or "")
    if not validation_name or cohort not in {
        "Validation",
        "Blinded Holdout",
        "Ongoing QC",
    }:
        frappe.throw("This component is not part of an open governed reviewer cohort")
    normalized_direction = str(direction or "next").strip().lower()
    if normalized_direction not in {"next", "previous"}:
        frappe.throw("Direction must be next or previous")

    validation, candidates = _pending_reviews_for_reviewer(validation_name, cohort)
    current_key = (int(review.tiered_sample_order or 0), str(review.name))
    before = [
        row
        for row in candidates
        if (int(row.tiered_sample_order or 0), str(row.name)) < current_key
    ]
    after = [
        row
        for row in candidates
        if (int(row.tiered_sample_order or 0), str(row.name)) > current_key
    ]
    ordered = (
        after + before
        if normalized_direction == "next"
        else list(reversed(before)) + list(reversed(after))
    )
    target = ordered[0] if ordered else None
    return {
        "review": str(target.name) if target else "",
        "validation": str(validation.name),
        "cohort": cohort,
    }


def _cohort_rows(validation_name: str, cohort: str) -> list[Any]:
    return frappe.get_all(
        COMPONENT_DOCTYPE,
        filters={
            "tiered_one_to_many_validation": validation_name,
            "tiered_governance_cohort": cohort,
        },
        fields=[
            "name",
            "review_status",
            "final_decision",
            "stale",
            "tiered_automation_status",
            "materialization_status",
            "identity_decision",
            "identity_groups_json",
        ],
        order_by="tiered_sample_order, name",
        limit_page_length=100_000,
    )


@frappe.whitelist(methods=["POST"])
def finalize_validation(validation_name: str) -> dict[str, Any]:
    _require_manager()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    if validation.status != "Reviewing":
        frappe.throw("Only a reviewing One-to-Many validation can be finalized")
    _require_frozen_provenance(validation)
    rows = _cohort_rows(validation.name, "Validation")
    stale = sum(bool(row.stale) for row in rows)
    final = [row for row in rows if row.review_status in FINAL_REVIEW_STATUSES and not row.stale]
    unresolved = len(rows) - len(final) - stale
    if unresolved:
        frappe.throw(
            f"{unresolved} validation component(s) still require two-reviewer resolution or adjudication"
        )
    gate = component_precision_gate(
        [str(row.final_decision or "") for row in final],
        stale_count=stale,
        unresolved_count=unresolved,
    )
    values = {
        "validation_complete_count": len(final),
        "validation_all_same_count": gate["all_same"],
        "validation_non_all_same_count": gate["non_all_same"],
        "validation_stale_count": stale,
        "validation_unresolved_count": unresolved,
        "validation_precision": gate["precision"] * 100,
        "validation_wilson_lower": gate["wilson_95"][0] * 100,
        "validation_wilson_upper": gate["wilson_95"][1] * 100,
        "validation_metrics_json": _json(gate),
        "status": "Awaiting Management Approval" if gate["passed"] else "Failed",
        "first_approval_status": (
            "Pending Management Review" if gate["passed"] else "Rejected"
        ),
        "error_summary": "" if gate["passed"] else ", ".join(gate["reasons"]),
    }
    validation.db_set(values, update_modified=False)
    frappe.db.commit()
    return {"validation": validation.name, **values, "gate": gate}


@frappe.whitelist(methods=["POST"])
def record_validation_decision(
    validation_name: str,
    decision: str,
    reason: str,
    confirm_validation_name: str,
) -> dict[str, Any]:
    _require_manager()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    if validation.status != "Awaiting Management Approval":
        frappe.throw("This validation is not awaiting management approval")
    _require_frozen_provenance(validation)
    if str(confirm_validation_name or "").strip() != validation.name:
        frappe.throw("Type the exact validation ID to confirm the decision")
    decision = str(decision or "").strip()
    if decision not in {"Approve", "Reject"}:
        frappe.throw("Decision must be Approve or Reject")
    reason = str(reason or "").strip()
    if not reason:
        frappe.throw("A management decision reason is required")
    approved = decision == "Approve"
    values = {
        "status": "Approved" if approved else "Rejected",
        "first_approval_status": "Approved" if approved else "Rejected",
        "first_approved_at": now_datetime(),
        "first_approved_by": frappe.session.user,
        "first_approval_reason": reason,
    }
    validation.db_set(values, update_modified=False)
    frappe.db.commit()
    return {"validation": validation.name, **values}


def _component_automation_plan(review: Any) -> dict[str, Any]:
    recommendations = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={
            "canary_run": review.canary_run,
            "cluster_fingerprint": review.cluster_fingerprint,
        },
        fields=[
            "name",
            "left_record",
            "right_record",
            "left_modified_at",
            "right_modified_at",
            "left_identity_fingerprint",
            "right_identity_fingerprint",
            "safety_reasons_json",
        ],
        order_by="name",
        limit_page_length=100_000,
    )
    record_ids = sorted(
        {
            str(item)
            for row in recommendations
            for item in (row.left_record, row.right_record)
        }
    )
    expected_modified: dict[str, str] = {}
    fingerprint_values: list[tuple[str, str]] = []
    for row in recommendations:
        for record_id, modified, fingerprint in (
            (row.left_record, row.left_modified_at, row.left_identity_fingerprint),
            (row.right_record, row.right_modified_at, row.right_identity_fingerprint),
        ):
            key = str(record_id)
            value = str(modified or "")
            prior = expected_modified.setdefault(key, value)
            if prior != value:
                frappe.throw("Component contains inconsistent frozen timestamps")
            fingerprint_values.append((key, str(fingerprint or "")))
    expected = expected_identity_fingerprints(fingerprint_values)
    canary = frappe.get_doc(CANARY_DOCTYPE, review.canary_run)
    return {
        "canary": canary,
        "record_ids": record_ids,
        "expected_modified": expected_modified,
        "expected_fingerprints": expected,
        "recommendations": recommendations,
    }


def _apply_component(review_name: str, batch_name: str) -> dict[str, Any]:
    review = frappe.get_doc(COMPONENT_DOCTYPE, review_name)
    if review.stale or _component_stale(review):
        frappe.throw("component_stale")
    plan = _component_automation_plan(review)
    result = materialize_identity(
        origin="Tiered One-to-Many Automated",
        origin_doctype=COMPONENT_DOCTYPE,
        origin_document=review.name,
        policy_snapshot_json=plan["canary"].policy_snapshot_json,
        policy_snapshot_sha256=plan["canary"].policy_snapshot_sha256,
        matching_policy=plan["canary"].matching_policy,
        record_ids=plan["record_ids"],
        groups=[plan["record_ids"]],
        expected_fingerprints=plan["expected_fingerprints"],
        expected_modified=plan["expected_modified"],
        reason_codes=["validated_tiered_one_to_many_component"],
        review_context={
            "component_fingerprint": review.cluster_fingerprint,
            "validation": review.tiered_one_to_many_validation,
            "automation_batch": batch_name,
        },
    )
    frappe.db.set_value(
        COMPONENT_DOCTYPE,
        review.name,
        {
            "materialization_status": "Applied",
            "identity_decision": result["identity_decision"],
            "identity_groups_json": _json(result.get("identity_groups") or []),
            "materialization_error": None,
            "tiered_automation_status": "Applied",
        },
        update_modified=False,
    )
    return result


def _apply_human_validation_component(review_name: str) -> dict[str, Any]:
    """Activate an already-final two-reviewer validation decision once authorized."""
    from db_connector.api_identity_human import _component_materialization_plan

    review = frappe.get_doc(COMPONENT_DOCTYPE, review_name)
    if review.review_status not in FINAL_REVIEW_STATUSES or not review.final_decision:
        frappe.throw("validation_component_not_final")
    if review.stale or _component_stale(review):
        frappe.throw("component_stale")
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
        reason_codes=[
            "prospective_validation_"
            + str(review.final_decision).casefold().replace(" ", "_")
        ],
        review_context={
            "final_decision": review.final_decision,
            "tiered_one_to_many_validation": review.tiered_one_to_many_validation,
        },
    )
    frappe.db.set_value(
        COMPONENT_DOCTYPE,
        review.name,
        {
            "materialization_status": "Applied",
            "identity_decision": result["identity_decision"],
            "identity_groups_json": _json(result.get("identity_groups") or []),
            "materialization_error": None,
            "tiered_automation_status": "Applied",
        },
        update_modified=False,
    )
    return result


def _create_batch(
    validation: Any,
    review_names: Iterable[str],
    *,
    batch_type: str,
    seed_suffix: str,
) -> Any:
    names = tuple(sorted({str(name) for name in review_names if str(name)}))
    if not names:
        frappe.throw("No eligible one-to-many components were selected")
    selection_seed = hashlib.sha256(
        f"{validation.validation_seed}\x1f{seed_suffix}".encode()
    ).hexdigest()
    return frappe.get_doc(
        {
            "doctype": BATCH_DOCTYPE,
            "validation": validation.name,
            "canary_run": validation.canary_run,
            "batch_type": batch_type,
            "status": "Queued",
            "selection_seed": selection_seed,
            "selection_manifest_sha256": _sha(names),
            "selected_component_count": len(names),
            "created_by": frappe.session.user,
            "component_reviews_json": _json(names),
        }
    ).insert(ignore_permissions=True)


@frappe.whitelist(methods=["POST"])
def start_holdout_rollout(validation_name: str) -> dict[str, Any]:
    _require_manager()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    if validation.status not in {"Approved", "Holdout Applying"}:
        frappe.throw("Approve the prospective validation before creating holdout rollout")
    _require_frozen_provenance(validation)
    if not materialization_enabled(automated=True, channel="one_to_many"):
        frappe.throw("Live Identity Materialization is disabled or One-to-Many automation is paused")
    if validation.holdout_batch:
        batch = frappe.get_doc(BATCH_DOCTYPE, validation.holdout_batch)
        return {"validation": validation.name, "batch": batch.name, "status": batch.status}
    rows = _cohort_rows(validation.name, "Blinded Holdout")
    names = [
        str(row.name)
        for row in rows
        if not row.stale and row.review_status == "Unreviewed"
    ]
    if len(names) < MINIMUM_ELIGIBLE_HOLDOUT:
        frappe.throw(
            f"At least {MINIMUM_ELIGIBLE_HOLDOUT} current blinded holdout components are required"
        )
    batch = _create_batch(
        validation,
        names,
        batch_type="Blinded Holdout Rollout",
        seed_suffix="holdout-rollout",
    )
    for name in names:
        frappe.db.set_value(
            COMPONENT_DOCTYPE,
            name,
            {
                "tiered_automation_batch": batch.name,
                "tiered_automation_status": "Rollout Planned",
            },
            update_modified=False,
        )
    validation.db_set(
        {
            "status": "Holdout Applying",
            "holdout_status": "Applying",
            "holdout_batch": batch.name,
        },
        update_modified=False,
    )
    frappe.db.commit()
    frappe.enqueue(
        "db_connector.api_tiered_one_to_many.apply_one_to_many_batch",
        queue="long",
        timeout=21_600,
        enqueue_after_commit=True,
        job_id=f"ccd-one-to-many-{batch.name}",
        batch_name=batch.name,
    )
    return {"validation": validation.name, "batch": batch.name, "status": "Queued"}


@frappe.whitelist(methods=["POST"])
def start_validation_label_activation(validation_name: str) -> dict[str, Any]:
    _require_manager()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    if validation.status != "Authorized" or validation.second_approval_status != "Approved":
        frappe.throw("Complete the blinded holdout and second approval first")
    _require_frozen_provenance(validation)
    if not materialization_enabled(automated=False):
        frappe.throw("Live Identity Materialization is disabled")
    rows = _cohort_rows(validation.name, "Validation")
    names = [
        str(row.name)
        for row in rows
        if row.review_status in FINAL_REVIEW_STATUSES
        and not row.stale
        and row.materialization_status not in {"Applied", "Corrected"}
    ]
    if not names:
        return {"status": "No Pending Validation Decisions", "batch": ""}
    existing = frappe.db.get_value(
        BATCH_DOCTYPE,
        {
            "validation": validation.name,
            "batch_type": "Validation Label Activation",
            "status": ["in", ["Queued", "Applying", "Applied", "Applied with Exceptions"]],
        },
        "name",
    )
    if existing:
        return {
            "status": str(frappe.db.get_value(BATCH_DOCTYPE, existing, "status")),
            "batch": str(existing),
            "existing": True,
        }
    batch = _create_batch(
        validation,
        names,
        batch_type="Validation Label Activation",
        seed_suffix="validation-label-activation",
    )
    for name in names:
        frappe.db.set_value(
            COMPONENT_DOCTYPE,
            name,
            {
                "tiered_automation_batch": batch.name,
                "tiered_automation_status": "Rollout Planned",
            },
            update_modified=False,
        )
    frappe.db.commit()
    frappe.enqueue(
        "db_connector.api_tiered_one_to_many.apply_one_to_many_batch",
        queue="long",
        timeout=21_600,
        enqueue_after_commit=True,
        job_id=f"ccd-one-to-many-{batch.name}",
        batch_name=batch.name,
    )
    return {"status": "Queued", "batch": batch.name, "existing": False}


def _weekly_qc_remaining() -> int:
    from db_connector.api_splink_automation import _weekly_assigned_count

    return max(20 - int(_weekly_assigned_count()), 0)


def _one_to_many_qc_capacity_mode(
    validation_name: str | None = None,
) -> dict[str, Any]:
    selected_validation = str(
        validation_name
        or frappe.db.get_single_value(
            SETTINGS_DOCTYPE, "tiered_one_to_many_validation"
        )
        or ""
    )
    if not selected_validation:
        return {
            "capacity_gate_required": True,
            "mode": "Capacity Gated",
            "risk_accepted": False,
            "event": "",
            "reason": "",
            "actor": "",
            "event_at": "",
            "validation": "",
        }
    rows = frappe.get_all(
        IDENTITY_EVENT_DOCTYPE,
        filters={
            "entity_doctype": VALIDATION_DOCTYPE,
            "entity_name": selected_validation,
            "event_type": ["in", ["Enable", "Disable"]],
        },
        fields=[
            "name",
            "to_status",
            "reason",
            "actor",
            "event_at",
            "metadata_json",
        ],
        order_by="event_at desc, creation desc",
        limit_page_length=100,
    )
    latest = None
    for row in rows:
        try:
            metadata = json.loads(row.metadata_json or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("control") == QC_CAPACITY_GATE_CONTROL:
            latest = row
            break
    gate_required = not latest or str(latest.to_status or "") != "Non-Blocking"
    return {
        "capacity_gate_required": gate_required,
        "mode": "Capacity Gated" if gate_required else "Non-Blocking",
        "risk_accepted": not gate_required,
        "event": str(latest.name) if latest else "",
        "reason": str(latest.reason or "") if latest else "",
        "actor": str(latest.actor or "") if latest else "",
        "event_at": str(latest.event_at or "") if latest else "",
        "validation": selected_validation,
    }


@frappe.whitelist()
def get_one_to_many_qc_capacity_mode() -> dict[str, Any]:
    _require_reviewer()
    return _one_to_many_qc_capacity_mode()


@frappe.whitelist(methods=["POST"])
def set_one_to_many_qc_capacity_gate(
    enabled: int | str,
    reason: str,
    confirm_phrase: str,
) -> dict[str, Any]:
    """Govern the explicit risk acceptance that decouples QC from writes."""
    _require_manager()
    desired_gate = str(enabled or "0").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    exact = (
        "RESTORE GATED ONE-TO-MANY QC"
        if desired_gate
        else "ACCEPT NON-BLOCKING ONE-TO-MANY QC RISK"
    )
    if str(confirm_phrase or "").strip() != exact:
        frappe.throw(f"Type {exact} exactly to confirm")
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        frappe.throw("A management risk decision reason is required")
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    validation_name = str(settings.tiered_one_to_many_validation or "")
    if not desired_gate:
        if not validation_name:
            frappe.throw("No authorized One-to-Many validation is configured")
        validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
        if validation.status != "Authorized" or validation.second_approval_status != "Approved":
            frappe.throw("Complete One-to-Many validation and holdout approval first")
        _require_frozen_provenance(validation)
    current = _one_to_many_qc_capacity_mode(validation_name)
    target_status = "Capacity Gated" if desired_gate else "Non-Blocking"
    if current["mode"] == target_status:
        return {**current, "unchanged": True}
    revision = int(settings.automation_control_revision or 0) + 1
    event = _append_event(
        entity_doctype=VALIDATION_DOCTYPE,
        entity_name=validation_name,
        event_type="Enable" if desired_gate else "Disable",
        reason=normalized_reason,
        nonce=hashlib.sha256(
            f"one-to-many-qc-capacity-gate\x1f{revision}\x1f{desired_gate}\x1f{normalized_reason}".encode()
        ).hexdigest(),
        from_status=str(current["mode"]),
        to_status=target_status,
        metadata={
            "control": QC_CAPACITY_GATE_CONTROL,
            "revision": revision,
            "validation": validation_name,
            "risk_accepted": not desired_gate,
            "effect": (
                "QC remains best effort and capacity shortfall does not block identity writes"
                if not desired_gate
                else "Complete requested QC capacity is required before identity writes"
            ),
        },
    )
    _set_settings({"automation_control_revision": revision})
    frappe.db.commit()
    return {
        "capacity_gate_required": desired_gate,
        "mode": target_status,
        "risk_accepted": not desired_gate,
        "event": event,
        "reason": normalized_reason,
        "actor": frappe.session.user,
        "event_at": str(now_datetime()),
    }


def _bulk_candidates(validation: Any) -> list[str]:
    inventory = _component_inventory(
        str(validation.canary_run), int(validation.maximum_component_size)
    )
    reserved = set(
        frappe.get_all(
            COMPONENT_DOCTYPE,
            filters={
                "tiered_one_to_many_validation": validation.name,
                "tiered_governance_cohort": ["in", ["Validation", "Blinded Holdout"]],
            },
            pluck="name",
            limit_page_length=100_000,
        )
    )
    return sorted(
        str(row["name"])
        for row in inventory["eligible"]
        if str(row["name"]) not in reserved
    )


@frappe.whitelist()
def preview_bulk_cycle(validation_name: str | None = None) -> dict[str, Any]:
    _require_manager()
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    selected = str(validation_name or settings.tiered_one_to_many_validation or "")
    if not selected:
        frappe.throw("No authorized One-to-Many validation is configured")
    validation = frappe.get_doc(VALIDATION_DOCTYPE, selected)
    if validation.status != "Stale":
        _require_frozen_provenance(validation)
    candidates = _bulk_candidates(validation) if validation.status == "Authorized" else []
    limit = max(int(settings.tiered_one_to_many_components_per_run or 10), 1)
    selected_count = min(len(candidates), limit)
    percent = float(settings.tiered_one_to_many_qc_percent or 10)
    capacity_mode = _one_to_many_qc_capacity_mode(validation.name)
    remaining_capacity = _weekly_qc_remaining()
    qc_plan = ongoing_qc_plan(
        selected_count,
        percent,
        remaining_capacity,
        capacity_gate_required=bool(capacity_mode["capacity_gate_required"]),
    )
    blockers = []
    if validation.status != "Authorized":
        blockers.append("validation_not_authorized")
    if not settings.automatic_tiered_one_to_many_enabled:
        blockers.append("one_to_many_automation_disabled")
    if settings.tiered_one_to_many_automation_paused:
        blockers.append("one_to_many_automation_paused")
    if not settings.materialization_enabled:
        blockers.append("live_materialization_disabled")
    if qc_plan["blocked"]:
        blockers.append(
            f"shared_weekly_qc_capacity:{qc_plan['assigned']}:{qc_plan['requested']}"
        )
    return {
        "zero_write": True,
        "validation": validation.name,
        "eligible_component_count": len(candidates),
        "selected_component_count": selected_count,
        "requested_qc_sample_count": qc_plan["requested"],
        "assignable_qc_sample_count": qc_plan["assigned"],
        "unassigned_qc_sample_count": qc_plan["unassigned"],
        "shared_weekly_qc_remaining": remaining_capacity,
        "qc_capacity_mode": capacity_mode["mode"],
        "qc_capacity_gate_required": capacity_mode["capacity_gate_required"],
        "qc_risk_acceptance_event": capacity_mode["event"],
        "blockers": blockers,
    }


@frappe.whitelist(methods=["POST"])
def start_bulk_cycle() -> dict[str, Any]:
    _require_manager()
    preview = preview_bulk_cycle()
    if preview["blockers"]:
        frappe.throw("One-to-Many bulk cycle is blocked: " + ", ".join(preview["blockers"]))
    if not preview["selected_component_count"]:
        return {"status": "No Eligible Components", **preview}
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    validation = frappe.get_doc(
        VALIDATION_DOCTYPE, settings.tiered_one_to_many_validation
    )
    candidates = _bulk_candidates(validation)
    cycle_index = frappe.db.count(
        BATCH_DOCTYPE,
        {"validation": validation.name, "batch_type": "Scheduled Bulk One-to-Many"},
    ) + 1
    ranked = sorted(
        candidates,
        key=lambda name: (
            hashlib.sha256(
                f"{validation.validation_seed}\x1fbulk:{cycle_index}\x1f{name}".encode()
            ).hexdigest(),
            name,
        ),
    )
    names = ranked[: int(preview["selected_component_count"])]
    batch = _create_batch(
        validation,
        names,
        batch_type="Scheduled Bulk One-to-Many",
        seed_suffix=f"bulk:{cycle_index}",
    )
    for name in names:
        frappe.db.set_value(
            COMPONENT_DOCTYPE,
            name,
            {
                "tiered_one_to_many_validation": validation.name,
                "tiered_governance_cohort": "Scheduled Bulk",
                "tiered_automation_batch": batch.name,
                "tiered_automation_status": "Rollout Planned",
            },
            update_modified=False,
        )
    _set_settings(
        {
            "last_tiered_one_to_many_batch": batch.name,
            "last_tiered_one_to_many_status": "Queued",
            "last_tiered_one_to_many_error": None,
        }
    )
    validation.db_set("last_automation_batch", batch.name, update_modified=False)
    frappe.db.commit()
    frappe.enqueue(
        "db_connector.api_tiered_one_to_many.apply_one_to_many_batch",
        queue="long",
        timeout=21_600,
        enqueue_after_commit=True,
        job_id=f"ccd-one-to-many-{batch.name}",
        batch_name=batch.name,
    )
    return {"status": "Queued", "batch": batch.name, **preview}


def apply_one_to_many_batch(batch_name: str) -> dict[str, Any]:
    batch = frappe.get_doc(BATCH_DOCTYPE, batch_name)
    validation = frappe.get_doc(VALIDATION_DOCTYPE, batch.validation)
    _require_frozen_provenance(validation)
    if batch.status in {"Applied", "Applied with Exceptions"}:
        return {"batch": batch.name, "status": batch.status}
    if not materialization_enabled(automated=True, channel="one_to_many"):
        frappe.throw("One-to-Many materialization became disabled before Apply")
    if batch.batch_type == "Scheduled Bulk One-to-Many":
        settings = frappe.get_single(SETTINGS_DOCTYPE)
        if not settings.automatic_tiered_one_to_many_enabled:
            frappe.throw("One-to-Many bulk automation was disabled before Apply")
        if str(settings.tiered_one_to_many_validation or "") != str(validation.name):
            frappe.throw("The authorized One-to-Many validation changed before Apply")
    names = tuple(json.loads(batch.component_reviews_json or "[]"))
    if _sha(names) != str(batch.selection_manifest_sha256):
        frappe.throw("The frozen one-to-many batch manifest is corrupt")
    batch.db_set(
        {"status": "Applying", "started_at": now_datetime()},
        update_modified=False,
    )
    frappe.db.commit()
    applied: list[str] = []
    exceptions: list[dict[str, str]] = []
    for name in names:
        try:
            if batch.batch_type == "Validation Label Activation":
                _apply_human_validation_component(str(name))
            else:
                _apply_component(str(name), batch.name)
            frappe.db.commit()
            applied.append(str(name))
        except Exception as exc:
            frappe.db.rollback()
            message = f"{type(exc).__name__}:{str(exc)}"[:500]
            exceptions.append({"review": str(name), "error": message})
            frappe.db.set_value(
                COMPONENT_DOCTYPE,
                str(name),
                {
                    "tiered_automation_status": "Exception",
                    "materialization_status": "Exception",
                    "materialization_error": message[:140],
                },
                update_modified=False,
            )
            frappe.db.commit()

    qc_names: tuple[str, ...] = tuple()
    if batch.batch_type == "Blinded Holdout Rollout" and applied:
        settings = frappe.get_single(SETTINGS_DOCTYPE)
        assigned_at = now_datetime()
        due = add_days(assigned_at, int(settings.qc_sla_days or 14))
        for name in applied:
            frappe.db.set_value(
                COMPONENT_DOCTYPE,
                name,
                {
                    "tiered_qc_assigned_at": assigned_at,
                    "tiered_qc_due_at": due,
                },
                update_modified=False,
            )
    if batch.batch_type == "Scheduled Bulk One-to-Many" and applied:
        settings = frappe.get_single(SETTINGS_DOCTYPE)
        percent = float(settings.tiered_one_to_many_qc_percent or 10)
        capacity_mode = _one_to_many_qc_capacity_mode(validation.name)
        qc_plan = ongoing_qc_plan(
            len(applied),
            percent,
            _weekly_qc_remaining(),
            capacity_gate_required=bool(capacity_mode["capacity_gate_required"]),
        )
        qc_count = int(qc_plan["assigned"])
        if qc_plan["blocked"]:
            # This should be prevented by the start-time preview. If capacity
            # changed concurrently, fail closed by pausing further cycles; the
            # completed identity writes remain visible and recoverable.
            _set_settings(
                {
                    "tiered_one_to_many_automation_paused": 1,
                    "tiered_one_to_many_pause_reason": "shared_qc_capacity_changed_during_apply",
                }
            )
        qc_names = deterministic_qc_sample(
            applied,
            seed=str(validation.validation_seed),
            batch_key=batch.name,
            count=qc_count,
        )
        due = add_days(now_datetime(), int(settings.qc_sla_days or 14))
        for order, name in enumerate(qc_names, start=1):
            frappe.db.set_value(
                COMPONENT_DOCTYPE,
                name,
                {
                    "tiered_governance_cohort": "Ongoing QC",
                    "tiered_automation_status": "QC Selected",
                    "tiered_sample_order": order,
                    "tiered_qc_assigned_at": now_datetime(),
                    "tiered_qc_due_at": due,
                },
                update_modified=False,
            )
        if qc_plan["unassigned"] and not qc_plan["capacity_gate_required"]:
            _append_event(
                entity_doctype=BATCH_DOCTYPE,
                entity_name=batch.name,
                event_type="QC Assign",
                reason="risk_accepted_non_blocking_qc_capacity_shortfall",
                nonce=hashlib.sha256(
                    f"one-to-many-unassigned-qc\x1f{batch.name}\x1f{qc_plan['requested']}\x1f{qc_count}".encode()
                ).hexdigest(),
                from_status=f"Requested {qc_plan['requested']}",
                to_status=f"Assigned {qc_count}",
                metadata={
                    "channel": "Tiered One-to-Many",
                    "validation": validation.name,
                    "requested_qc": qc_plan["requested"],
                    "assigned_qc": qc_count,
                    "risk_accepted_unassigned_qc": qc_plan["unassigned"],
                    "capacity_mode": capacity_mode["mode"],
                    "risk_acceptance_event": capacity_mode["event"],
                },
            )
    status = "Applied with Exceptions" if exceptions else "Applied"
    batch.db_set(
        {
            "status": status,
            "applied_component_count": len(applied),
            "exception_component_count": len(exceptions),
            "qc_selected_count": len(qc_names),
            "exception_manifest_json": _json(exceptions),
            "completed_at": now_datetime(),
        },
        update_modified=False,
    )
    if batch.batch_type == "Blinded Holdout Rollout":
        validation.db_set(
            {
                "status": (
                    "Holdout Reviewing"
                    if len(applied) >= MINIMUM_ELIGIBLE_HOLDOUT
                    else "Failed"
                ),
                "holdout_status": (
                    "Reviewing"
                    if len(applied) >= MINIMUM_ELIGIBLE_HOLDOUT
                    else "Failed"
                ),
                "holdout_applied_count": len(applied),
                "holdout_exception_count": len(exceptions),
                "error_summary": (
                    ""
                    if len(applied) >= MINIMUM_ELIGIBLE_HOLDOUT
                    else f"eligible_holdout_below_{MINIMUM_ELIGIBLE_HOLDOUT}"
                ),
            },
            update_modified=False,
        )
    elif batch.batch_type == "Scheduled Bulk One-to-Many":
        validation.db_set(
            "ongoing_qc_selected_count",
            int(validation.ongoing_qc_selected_count or 0) + len(qc_names),
            update_modified=False,
        )
        _set_settings(
            {
                "last_tiered_one_to_many_status": status,
                "last_tiered_one_to_many_error": (
                    f"{len(exceptions)} component exception(s)" if exceptions else None
                ),
            }
        )
    frappe.db.commit()
    return {
        "batch": batch.name,
        "status": status,
        "applied": len(applied),
        "exceptions": len(exceptions),
        "qc_selected": len(qc_names),
    }


@frappe.whitelist(methods=["POST"])
def finalize_holdout_qc(validation_name: str) -> dict[str, Any]:
    _require_manager()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    if validation.status != "Holdout Reviewing":
        frappe.throw("The blinded holdout is not open for finalization")
    _require_frozen_provenance(validation)
    validation_rows = _cohort_rows(validation.name, "Validation")
    holdout_rows = [
        row
        for row in _cohort_rows(validation.name, "Blinded Holdout")
        if row.tiered_automation_status in {"Applied", "QC Complete"}
    ]
    validation_final = [
        row
        for row in validation_rows
        if row.review_status in FINAL_REVIEW_STATUSES and not row.stale
    ]
    holdout_final = [
        row
        for row in holdout_rows
        if row.review_status in FINAL_REVIEW_STATUSES and not row.stale
    ]
    holdout_stale = sum(bool(row.stale) for row in holdout_rows)
    holdout_unresolved = len(holdout_rows) - len(holdout_final) - holdout_stale
    if holdout_unresolved:
        frappe.throw(
            f"{holdout_unresolved} applied holdout component(s) still require two-reviewer QC or adjudication"
        )
    gate = holdout_and_combined_gate(
        [str(row.final_decision or "") for row in validation_final],
        [str(row.final_decision or "") for row in holdout_final],
        validation_stale_count=int(validation.validation_stale_count or 0),
        validation_unresolved_count=int(validation.validation_unresolved_count or 0),
        holdout_stale_count=holdout_stale,
        holdout_unresolved_count=holdout_unresolved,
    )
    values = {
        "holdout_qc_complete_count": len(holdout_final),
        "holdout_qc_all_same_count": gate["holdout"]["all_same"],
        "holdout_qc_non_all_same_count": gate["holdout"]["non_all_same"],
        "holdout_metrics_json": _json(gate),
        "status": (
            "Holdout Awaiting Management Approval"
            if gate["combined"]["passed"]
            else "Failed"
        ),
        "holdout_status": (
            "Awaiting Management Approval"
            if gate["combined"]["passed"]
            else "Failed"
        ),
        "second_approval_status": (
            "Pending Management Review"
            if gate["combined"]["passed"]
            else "Rejected"
        ),
        "error_summary": (
            "" if gate["combined"]["passed"] else ", ".join(gate["combined"]["reasons"])
        ),
    }
    validation.db_set(values, update_modified=False)
    frappe.db.commit()
    return {"validation": validation.name, **values, "gate": gate}


@frappe.whitelist(methods=["POST"])
def record_holdout_decision(
    validation_name: str,
    decision: str,
    reason: str,
    confirm_validation_name: str,
) -> dict[str, Any]:
    _require_manager()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    if validation.status != "Holdout Awaiting Management Approval":
        frappe.throw("The holdout is not awaiting the second management decision")
    _require_frozen_provenance(validation)
    if str(confirm_validation_name or "").strip() != validation.name:
        frappe.throw("Type the exact validation ID to confirm the decision")
    decision = str(decision or "").strip()
    if decision not in {"Approve", "Reject"}:
        frappe.throw("Decision must be Approve or Reject")
    reason = str(reason or "").strip()
    if not reason:
        frappe.throw("A management decision reason is required")
    approved = decision == "Approve"
    values = {
        "status": "Authorized" if approved else "Rejected",
        "holdout_status": "Completed" if approved else "Failed",
        "second_approval_status": "Approved" if approved else "Rejected",
        "second_approved_at": now_datetime(),
        "second_approved_by": frappe.session.user,
        "second_approval_reason": reason,
    }
    validation.db_set(values, update_modified=False)
    if approved:
        _set_settings(
            {
                "tiered_one_to_many_validation": validation.name,
                "maximum_tiered_one_to_many_component_size": validation.maximum_component_size,
                "automatic_tiered_one_to_many_enabled": 0,
                "tiered_one_to_many_automation_paused": 0,
                "tiered_one_to_many_pause_reason": None,
            }
        )
    frappe.db.commit()
    return {"validation": validation.name, **values}


@frappe.whitelist(methods=["POST"])
def set_one_to_many_automation(
    enabled: int | str,
    reason: str,
    confirm_phrase: str,
) -> dict[str, Any]:
    _require_manager()
    desired = str(enabled or "0").casefold() in {"1", "true", "yes", "on"}
    phrase = "ENABLE ONE-TO-MANY AUTOMATION" if desired else "DISABLE ONE-TO-MANY AUTOMATION"
    if str(confirm_phrase or "").strip() != phrase:
        frappe.throw(f"Type {phrase} exactly")
    reason = str(reason or "").strip()
    if not reason:
        frappe.throw("A control-change reason is required")
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if desired:
        if not settings.materialization_enabled:
            frappe.throw("Enable Live Identity Materialization first")
        if not settings.tiered_one_to_many_validation:
            frappe.throw("No authorized One-to-Many validation is configured")
        validation = frappe.get_doc(
            VALIDATION_DOCTYPE, settings.tiered_one_to_many_validation
        )
        if validation.status != "Authorized" or validation.second_approval_status != "Approved":
            frappe.throw("The configured One-to-Many validation is not authorized")
        _require_frozen_provenance(validation)
        open_investigations = frappe.db.count(
            INVESTIGATION_DOCTYPE,
            {"automation_channel": "Tiered One-to-Many", "status": "Open"},
        )
        if open_investigations:
            frappe.throw(
                f"Resolve {open_investigations} open One-to-Many QC investigation(s) before enabling automation"
            )
    _set_settings(
        {
            "automatic_tiered_one_to_many_enabled": int(desired),
            "tiered_one_to_many_automation_paused": 0 if desired else settings.tiered_one_to_many_automation_paused,
            "tiered_one_to_many_pause_reason": None if desired else settings.tiered_one_to_many_pause_reason,
            "last_tiered_one_to_many_status": "Enabled" if desired else "Disabled",
            "last_tiered_one_to_many_error": None,
        }
    )
    frappe.db.commit()
    return {"status": "Enabled" if desired else "Disabled", "reason": reason}


def _identity_group_for_review(review: Any) -> str:
    try:
        groups = json.loads(review.identity_groups_json or "[]")
    except (TypeError, ValueError):
        groups = []
    return str(groups[0]) if groups else ""


def _open_component_qc_investigation(review: Any, reason: str) -> str:
    key = hashlib.sha256(f"{review.name}\x1fone-to-many-qc-failure".encode()).hexdigest()
    existing = frappe.db.get_value(
        INVESTIGATION_DOCTYPE, {"investigation_key": key}, "name"
    )
    if existing:
        return str(existing)
    return frappe.get_doc(
        {
            "doctype": INVESTIGATION_DOCTYPE,
            "investigation_key": key,
            "automation_channel": "Tiered One-to-Many",
            "canary_run": review.canary_run,
            "component_review": review.name,
            "tiered_one_to_many_validation": review.tiered_one_to_many_validation,
            "identity_decision": review.identity_decision or None,
            "identity_group": _identity_group_for_review(review) or None,
            "pause_scope": str(review.tiered_one_to_many_validation),
            "status": "Open",
            "reason": reason,
            "opened_at": now_datetime(),
            "opened_by": frappe.session.user or "Administrator",
        }
    ).insert(ignore_permissions=True).name


def record_component_review_outcome(review_name: str) -> dict[str, Any]:
    """Keep validation/QC labels evidence-only; never rematerialize them as human decisions."""
    review = frappe.get_doc(COMPONENT_DOCTYPE, review_name)
    if not review.tiered_one_to_many_validation:
        return {"handled": False}
    if review.review_status not in FINAL_REVIEW_STATUSES or not review.final_decision:
        return {"handled": True, "status": "Not Final"}
    cohort = str(review.tiered_governance_cohort or "")
    if cohort == "Validation":
        frappe.db.set_value(
            COMPONENT_DOCTYPE,
            review.name,
            "tiered_automation_status",
            "Validation Final",
            update_modified=False,
        )
        return {"handled": True, "status": "Validation Final"}
    if cohort not in {"Blinded Holdout", "Ongoing QC"}:
        return {"handled": True, "status": "Governance Evidence"}
    values = {
        "tiered_automation_status": "QC Complete",
        "tiered_qc_finalized_at": now_datetime(),
    }
    failure = str(review.final_decision) != "All Same"
    if failure:
        reason = f"confirmed_one_to_many_qc_{str(review.final_decision).casefold().replace(' ', '_')}:{review.name}"
        investigation = _open_component_qc_investigation(review, reason)
        from db_connector.api_identity_qc import _suspend_group_for_qc

        suspended = _suspend_group_for_qc(
            _identity_group_for_review(review) or None, investigation
        )
        values["tiered_qc_failure_action"] = (
            f"automation_paused;investigation={investigation};"
            f"memberships_needing_revalidation={suspended}"
        )
        _set_settings(
            {
                "tiered_one_to_many_automation_paused": 1,
                "tiered_one_to_many_pause_reason": reason,
            }
        )
    frappe.db.set_value(
        COMPONENT_DOCTYPE,
        review.name,
        values,
        update_modified=False,
    )
    validation = frappe.get_doc(
        VALIDATION_DOCTYPE, review.tiered_one_to_many_validation
    )
    if cohort == "Ongoing QC":
        complete = frappe.db.count(
            COMPONENT_DOCTYPE,
            {
                "tiered_one_to_many_validation": validation.name,
                "tiered_governance_cohort": "Ongoing QC",
                "tiered_automation_status": "QC Complete",
            },
        )
        failures = frappe.db.count(
            COMPONENT_DOCTYPE,
            {
                "tiered_one_to_many_validation": validation.name,
                "tiered_governance_cohort": "Ongoing QC",
                "tiered_automation_status": "QC Complete",
                "final_decision": ["!=", "All Same"],
            },
        )
        validation.db_set(
            {
                "ongoing_qc_complete_count": complete,
                "ongoing_qc_failure_count": failures,
            },
            update_modified=False,
        )
    return {
        "handled": True,
        "status": "QC Complete",
        "failure": failure,
    }


@frappe.whitelist()
def get_validation_dashboard(validation_name: str) -> dict[str, Any]:
    _require_reviewer()
    validation = frappe.get_doc(VALIDATION_DOCTYPE, validation_name)
    return {
        "validation": validation.name,
        "status": validation.status,
        "canary_run": validation.canary_run,
        "maximum_component_size": validation.maximum_component_size,
        "eligible_component_count": validation.eligible_component_count,
        "validation_target_count": validation.validation_target_count,
        "holdout_target_count": validation.holdout_target_count,
        "validation_complete_count": validation.validation_complete_count,
        "validation_all_same_count": validation.validation_all_same_count,
        "validation_non_all_same_count": validation.validation_non_all_same_count,
        "validation_wilson_lower": validation.validation_wilson_lower,
        "holdout_status": validation.holdout_status,
        "holdout_applied_count": validation.holdout_applied_count,
        "holdout_qc_complete_count": validation.holdout_qc_complete_count,
        "second_approval_status": validation.second_approval_status,
        "ongoing_qc_selected_count": validation.ongoing_qc_selected_count,
        "ongoing_qc_complete_count": validation.ongoing_qc_complete_count,
        "ongoing_qc_failure_count": validation.ongoing_qc_failure_count,
        "error_summary": validation.error_summary or "",
    }


def monitor_ongoing_qc() -> dict[str, Any]:
    """Pause only one-to-many automation for stale, overdue, or weak sampled QC."""
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    validation_name = str(settings.tiered_one_to_many_validation or "")
    if not validation_name or not frappe.db.exists(VALIDATION_DOCTYPE, validation_name):
        return {"status": "Not Configured", "assigned": 0}
    rows = frappe.get_all(
        COMPONENT_DOCTYPE,
        filters={
            "tiered_one_to_many_validation": validation_name,
            "tiered_governance_cohort": "Ongoing QC",
            "tiered_qc_assigned_at": ["is", "set"],
        },
        fields=[
            "name",
            "review_status",
            "final_decision",
            "stale",
            "tiered_qc_due_at",
            "tiered_qc_finalized_at",
        ],
        order_by="tiered_qc_finalized_at, tiered_qc_assigned_at, name",
        limit_page_length=100_000,
    )
    stale_count = 0
    current_rows = []
    for row in rows:
        if row.review_status not in FINAL_REVIEW_STATUSES and not row.stale:
            review = frappe.get_doc(COMPONENT_DOCTYPE, row.name)
            if _component_stale(review):
                frappe.db.set_value(
                    COMPONENT_DOCTYPE,
                    row.name,
                    {
                        "stale": 1,
                        "review_status": "Stale",
                        "tiered_automation_status": "Stale",
                    },
                    update_modified=False,
                )
                row.stale = 1
        stale_count += int(bool(row.stale))
        if not row.stale:
            current_rows.append(row)
    now = now_datetime()
    overdue = sum(
        row.review_status not in FINAL_REVIEW_STATUSES
        and bool(row.tiered_qc_due_at)
        and get_datetime(row.tiered_qc_due_at) < now
        for row in current_rows
    )
    finalized = [
        row for row in current_rows if row.review_status in FINAL_REVIEW_STATUSES
    ]
    all_same = sum(str(row.final_decision or "") == "All Same" for row in finalized)
    failures = len(finalized) - all_same
    window_size = max(int(settings.rolling_qc_window or 100), 1)
    window = finalized[-window_size:]
    window_same = sum(str(row.final_decision or "") == "All Same" for row in window)
    lower, upper = wilson_interval(window_same, len(window))
    pause_reasons = []
    if stale_count:
        pause_reasons.append(f"one_to_many_qc_stale:{stale_count}")
    if overdue:
        pause_reasons.append(f"one_to_many_qc_overdue:{overdue}")
    if len(window) >= window_size and lower < 0.95:
        pause_reasons.append(f"one_to_many_qc_wilson_below_0.95:{lower:.6f}")
    if failures:
        pause_reasons.append(f"one_to_many_qc_failures:{failures}")
    if pause_reasons:
        _set_settings(
            {
                "tiered_one_to_many_automation_paused": 1,
                "tiered_one_to_many_pause_reason": ";".join(pause_reasons),
            }
        )
    frappe.db.set_value(
        VALIDATION_DOCTYPE,
        validation_name,
        {
            "ongoing_qc_complete_count": len(finalized),
            "ongoing_qc_failure_count": failures,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "status": "Paused" if pause_reasons else "Monitoring",
        "assigned": len(rows),
        "complete": len(finalized),
        "failures": failures,
        "stale": stale_count,
        "overdue": overdue,
        "rolling_valid": len(window),
        "rolling_wilson_95": (lower, upper),
        "pause_reasons": pause_reasons,
    }
