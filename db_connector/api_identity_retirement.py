"""Audited retirement of identity state whose CCD Master source disappeared."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import frappe

from db_connector.api_identity_resolution import _append_event
from db_connector.fuzzy_matching.retirement import (
    FINAL_REVIEW_STATUSES,
    HISTORICAL_MATERIALIZATION_STATUSES,
    canonical_json,
    group_retirement_targets,
    stable_scope_fingerprint,
)


MASTER_DOCTYPE = "CCD Master"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
RETIREMENT_DOCTYPE = "CCD Identity Retirement Run"
HISTORICAL_SOURCE_RETIRED = "Historical Source Retired"
CHUNK_SIZE = 1_000


def _require_manager() -> None:
    if "System Manager" not in set(frappe.get_roles()):
        frappe.throw("System Manager role is required", frappe.PermissionError)


def _chunks(values: Iterable[str], size: int = CHUNK_SIZE):
    ordered = tuple(sorted({str(value) for value in values if str(value)}))
    for offset in range(0, len(ordered), size):
        yield ordered[offset : offset + size]


def _rows_by_name(doctype: str, names: Iterable[str], fields: list[str]) -> list[Any]:
    output: list[Any] = []
    for chunk in _chunks(names):
        output.extend(
            frappe.get_all(
                doctype,
                filters={"name": ["in", chunk]},
                fields=fields,
                limit_page_length=len(chunk),
            )
        )
    return output


def _existing_masters(record_ids: Iterable[str]) -> set[str]:
    output: set[str] = set()
    for chunk in _chunks(record_ids):
        output.update(
            str(value)
            for value in frappe.get_all(
                MASTER_DOCTYPE,
                filters={"name": ["in", chunk]},
                pluck="name",
                limit_page_length=len(chunk),
            )
        )
    return output


def _pair_orphans(
    doctype: str,
    fields: Iterable[str],
    retired_record_ids: Iterable[str] = (),
    *,
    include_existing_orphans: bool = True,
) -> list[Any]:
    retired = {str(value) for value in retired_record_ids if str(value)}
    selected = ", ".join(f"t.`{field}`" for field in fields)
    rows = (
        frappe.db.sql(
            f"""SELECT {selected},
                   CASE WHEN left_master.name IS NULL THEN 1 ELSE 0 END AS left_missing,
                   CASE WHEN right_master.name IS NULL THEN 1 ELSE 0 END AS right_missing
            FROM `tab{doctype}` t
            LEFT JOIN `tab{MASTER_DOCTYPE}` left_master ON left_master.name=t.left_record
            LEFT JOIN `tab{MASTER_DOCTYPE}` right_master ON right_master.name=t.right_record
            WHERE left_master.name IS NULL OR right_master.name IS NULL""",
            as_dict=True,
        )
        if include_existing_orphans
        else []
    )
    by_name = {str(row.name): row for row in rows}
    field_list = list(fields)
    for chunk in _chunks(retired):
        for side in ("left_record", "right_record"):
            for row in frappe.get_all(
                doctype,
                filters={side: ["in", chunk]},
                fields=field_list,
                limit_page_length=100_000,
            ):
                row.left_missing = int(str(row.left_record or "") in retired)
                row.right_missing = int(str(row.right_record or "") in retired)
                by_name[str(row.name)] = row
    return list(by_name.values())


def _json_record_ids(value: Any) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed if isinstance(item, str) and item}


def _json_rows_with_missing(
    doctype: str,
    json_field: str,
    *,
    filters: dict[str, Any] | None = None,
    extra_fields: Iterable[str] = (),
    retired_record_ids: Iterable[str] = (),
    target_only: bool = False,
) -> tuple[list[Any], set[str]]:
    fields = ["name", json_field, *extra_fields]
    rows = frappe.get_all(
        doctype,
        filters=filters or {},
        fields=fields,
        limit_page_length=100_000,
    )
    ids_by_name = {
        str(row.name): _json_record_ids(row.get(json_field)) for row in rows
    }
    all_ids = set().union(*ids_by_name.values()) if ids_by_name else set()
    retired = {str(value) for value in retired_record_ids if str(value)}
    existing = _existing_masters(all_ids)
    existing -= retired
    affected = [
        row
        for row in rows
        if (
            bool(ids_by_name[str(row.name)] & retired)
            if target_only
            else bool(ids_by_name[str(row.name)] - existing)
        )
    ]
    return affected, (all_ids & retired if target_only else all_ids - existing)


def _component_review_orphans(
    recommendations: Iterable[Any] = (),
    *,
    include_existing_orphans: bool = True,
) -> list[Any]:
    rows = (
        frappe.db.sql(
            f"""SELECT DISTINCT review.name, review.review_status, review.stale,
                           review.materialization_status
            FROM `tabCCD Match Component Review` review
            INNER JOIN `tabCCD Match Recommendation` recommendation
                ON recommendation.canary_run=review.canary_run
               AND recommendation.cluster_fingerprint=review.cluster_fingerprint
            LEFT JOIN `tab{MASTER_DOCTYPE}` left_master
                ON left_master.name=recommendation.left_record
            LEFT JOIN `tab{MASTER_DOCTYPE}` right_master
                ON right_master.name=recommendation.right_record
            WHERE left_master.name IS NULL OR right_master.name IS NULL""",
            as_dict=True,
        )
        if include_existing_orphans
        else []
    )
    by_name = {str(row.name): row for row in rows}
    linked_names = {
        str(row.component_review)
        for row in recommendations
        if str(row.component_review or "")
    }
    for row in _rows_by_name(
        "CCD Match Component Review",
        linked_names,
        ["name", "review_status", "stale", "materialization_status"],
    ):
        by_name[str(row.name)] = row
    return list(by_name.values())


def _current_group_rows(
    orphan_memberships: list[Any], retired_record_ids: Iterable[str] = ()
) -> tuple[list[Any], dict[str, Any]]:
    affected_groups = {str(row.identity_group) for row in orphan_memberships}
    rows: list[Any] = []
    if affected_groups:
        rows = []
        for chunk in _chunks(affected_groups):
            rows.extend(
                frappe.get_all(
                    "CCD Identity Membership",
                    filters={
                        "identity_group": ["in", chunk],
                        "status": ["in", ["Active", "Needs Revalidation"]],
                    },
                    fields=[
                        "name",
                        "identity_group",
                        "ccd_master",
                        "status",
                        "originating_decision",
                    ],
                    limit_page_length=100_000,
                )
            )
    existing = _existing_masters(str(row.ccd_master) for row in rows)
    existing -= {str(value) for value in retired_record_ids if str(value)}
    targets = group_retirement_targets((dict(row) for row in rows), existing)
    return rows, targets


def _unique(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if str(value or "")}))


def _matching_runs_for_sources(
    doctype: str, sources: Iterable[str]
) -> tuple[str, ...]:
    source_set = {str(value) for value in sources if str(value)}
    if not source_set:
        return ()
    rows = frappe.get_all(
        doctype,
        filters={"status": ["not in", ["Stale", "Superseded"]]},
        fields=["name", "policy_snapshot_json"],
        limit_page_length=10_000,
    )
    selected = []
    for row in rows:
        try:
            snapshot = json.loads(row.policy_snapshot_json or "{}")
        except (TypeError, ValueError):
            continue
        run_sources = {
            str(profile.get("source") or "")
            for profile in snapshot.get("source_profiles") or []
        }
        if source_set & run_sources:
            selected.append(str(row.name))
    return tuple(sorted(selected))


def _merge_rows_by_name(*groups: Iterable[Any]) -> list[Any]:
    output: dict[str, Any] = {}
    for rows in groups:
        for row in rows:
            output[str(row.name)] = row
    return list(output.values())


def _collect_state(
    retired_record_ids: Iterable[str] = (),
    *,
    scope_type: str = "Orphan Repair",
    scope_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    retired = {str(value) for value in retired_record_ids if str(value)}
    recommendations = _pair_orphans(
        "CCD Match Recommendation",
        [
            "name",
            "left_record",
            "right_record",
            "status",
            "rollout_state",
            "canary_run",
            "component_review",
            "activation_batch",
            "qc_selected",
            "qc_review_status",
            "qc_stale",
        ],
        retired,
        include_existing_orphans=not retired,
    )
    evaluation_pairs = _pair_orphans(
        "CCD Match Evaluation Pair",
        ["name", "left_record", "right_record", "evaluation_run", "stale"],
        retired,
        include_existing_orphans=not retired,
    )
    candidates = _pair_orphans(
        "CCD Match Review Candidate",
        [
            "name",
            "left_record",
            "right_record",
            "queue_run",
            "assigned_review_batch",
            "review_status",
            "stale",
            "materialization_status",
            "identity_decision",
        ],
        retired,
        include_existing_orphans=not retired,
    )
    scoped_canary_names: tuple[str, ...] = ()
    scoped_evaluation_names: tuple[str, ...] = ()
    scoped_queue_names: tuple[str, ...] = ()
    if retired:
        retired_sources = {
            str(row.ccd_reg_source)
            for row in _rows_by_name(
                MASTER_DOCTYPE, retired, ["name", "ccd_reg_source"]
            )
            if str(row.ccd_reg_source or "")
        }
        scoped_canary_names = _matching_runs_for_sources(
            "CCD Match Canary Run", retired_sources
        )
        scoped_evaluation_names = _matching_runs_for_sources(
            "CCD Match Evaluation Run", retired_sources
        )
        scoped_queue_names = _matching_runs_for_sources(
            "CCD Match Review Queue Run", retired_sources
        )
        if scoped_canary_names:
            recommendations = _merge_rows_by_name(
                recommendations,
                frappe.get_all(
                    "CCD Match Recommendation",
                    filters={"canary_run": ["in", scoped_canary_names]},
                    fields=[
                        "name", "left_record", "right_record", "status",
                        "rollout_state", "canary_run", "component_review",
                        "activation_batch", "qc_selected", "qc_review_status",
                        "qc_stale",
                    ],
                    limit_page_length=100_000,
                ),
            )
        if scoped_evaluation_names:
            evaluation_pairs = _merge_rows_by_name(
                evaluation_pairs,
                frappe.get_all(
                    "CCD Match Evaluation Pair",
                    filters={"evaluation_run": ["in", scoped_evaluation_names]},
                    fields=[
                        "name", "left_record", "right_record", "evaluation_run",
                        "stale",
                    ],
                    limit_page_length=100_000,
                ),
            )
        if scoped_queue_names:
            candidates = _merge_rows_by_name(
                candidates,
                frappe.get_all(
                    "CCD Match Review Candidate",
                    filters={"queue_run": ["in", scoped_queue_names]},
                    fields=[
                        "name", "left_record", "right_record", "queue_run",
                        "assigned_review_batch", "review_status", "stale",
                        "materialization_status", "identity_decision",
                    ],
                    limit_page_length=100_000,
                ),
            )
    component_reviews = _component_review_orphans(
        recommendations, include_existing_orphans=not retired
    )
    unified_membership_rows: list[Any] = []
    if frappe.db.table_exists("CCD Unified Person Membership"):
        if retired:
            for chunk in _chunks(retired):
                unified_membership_rows.extend(
                    frappe.get_all(
                        "CCD Unified Person Membership",
                        filters={"ccd_master": ["in", chunk], "status": "Active"},
                        fields=["name", "unified_person", "ccd_master", "status"],
                        limit_page_length=100_000,
                    )
                )
        else:
            unified_membership_rows = frappe.db.sql(
                f"""SELECT membership.name, membership.unified_person,
                           membership.ccd_master, membership.status
                      FROM `tabCCD Unified Person Membership` membership
                      LEFT JOIN `tab{MASTER_DOCTYPE}` master
                        ON master.name=membership.ccd_master
                     WHERE membership.status='Active' AND master.name IS NULL""",
                as_dict=True,
            )
    orphan_membership_rows = frappe.db.sql(
        f"""SELECT membership.name, membership.identity_group,
                   membership.ccd_master, membership.status,
                   membership.originating_decision
            FROM `tabCCD Identity Membership` membership
            LEFT JOIN `tab{MASTER_DOCTYPE}` master
                ON master.name=membership.ccd_master
            WHERE membership.status IN ('Active', 'Needs Revalidation')
              AND master.name IS NULL""",
        as_dict=True,
    ) if not retired else []
    orphan_memberships_by_name = {
        str(row.name): row for row in orphan_membership_rows
    }
    for chunk in _chunks(retired):
        for row in frappe.get_all(
            "CCD Identity Membership",
            filters={
                "ccd_master": ["in", chunk],
                "status": ["in", ["Active", "Needs Revalidation"]],
            },
            fields=[
                "name", "identity_group", "ccd_master", "status",
                "originating_decision",
            ],
            limit_page_length=100_000,
        ):
            orphan_memberships_by_name[str(row.name)] = row
    orphan_memberships = list(orphan_memberships_by_name.values())
    group_memberships, group_targets = _current_group_rows(
        orphan_memberships, retired
    )
    orphan_exclusion_rows = frappe.db.sql(
        f"""SELECT exclusion_row.name, exclusion_row.status,
                   exclusion_row.left_record, exclusion_row.right_record,
                   exclusion_row.originating_decision
            FROM `tabCCD Identity Exclusion` exclusion_row
            LEFT JOIN `tab{MASTER_DOCTYPE}` left_master
                ON left_master.name=exclusion_row.left_record
            LEFT JOIN `tab{MASTER_DOCTYPE}` right_master
                ON right_master.name=exclusion_row.right_record
            WHERE exclusion_row.status='Active'
              AND (left_master.name IS NULL OR right_master.name IS NULL)""",
        as_dict=True,
    ) if not retired else []
    orphan_exclusions_by_name = {
        str(row.name): row for row in orphan_exclusion_rows
    }
    for chunk in _chunks(retired):
        for side in ("left_record", "right_record"):
            for row in frappe.get_all(
                "CCD Identity Exclusion",
                filters={side: ["in", chunk], "status": "Active"},
                fields=[
                    "name", "status", "left_record", "right_record",
                    "originating_decision",
                ],
                limit_page_length=100_000,
            ):
                orphan_exclusions_by_name[str(row.name)] = row
    orphan_exclusions = list(orphan_exclusions_by_name.values())
    decisions, decision_missing = _json_rows_with_missing(
        "CCD Identity Decision",
        "participant_records_json",
        filters={"status": "Active"},
        extra_fields=["status"],
        retired_record_ids=retired,
        target_only=bool(retired),
    )
    overlaps, overlap_missing = _json_rows_with_missing(
        "CCD Identity Overlap Resolution",
        "participant_records_json",
        extra_fields=["source_population_status"],
        retired_record_ids=retired,
        target_only=bool(retired),
    )
    corrections, correction_missing = _json_rows_with_missing(
        "CCD Identity Correction",
        "participant_records_json",
        extra_fields=["source_population_status"],
        retired_record_ids=retired,
        target_only=bool(retired),
    )

    canary_names = _unique(
        [*scoped_canary_names, *(row.canary_run for row in recommendations)]
    )
    evaluation_run_names = _unique(
        [
            *scoped_evaluation_names,
            *(row.evaluation_run for row in evaluation_pairs),
        ]
    )
    queue_names = _unique(
        [*scoped_queue_names, *(row.queue_run for row in candidates)]
    )
    canary_rows = _rows_by_name("CCD Match Canary Run", canary_names, ["name", "status"])
    evaluation_runs = _rows_by_name(
        "CCD Match Evaluation Run", evaluation_run_names, ["name", "status"]
    )
    queue_rows = _rows_by_name(
        "CCD Match Review Queue Run", queue_names, ["name", "status"]
    )

    batch_names = set(
        _unique(row.activation_batch for row in recommendations)
    )
    for chunk in _chunks(canary_names):
        batch_names.update(
            str(value)
            for value in frappe.get_all(
                "CCD Identity Activation Batch",
                filters={"canary_run": ["in", chunk]},
                pluck="name",
                limit_page_length=100_000,
            )
        )
    activation_batches = _rows_by_name(
        "CCD Identity Activation Batch",
        batch_names,
        ["name", "status", "source_population_status"],
    )
    review_batch_names = set(
        _unique(row.assigned_review_batch for row in candidates)
    )
    review_batches = _rows_by_name(
        "CCD Match Review Batch", review_batch_names, ["name", "status"]
    )

    decision_names = _unique(row.name for row in decisions)
    group_names = _unique(row.identity_group for row in group_memberships)
    membership_names = _unique(row.name for row in group_memberships)
    exclusion_names = _unique(row.name for row in orphan_exclusions)
    recommendation_names = _unique(row.name for row in recommendations)
    all_qc_rows = frappe.get_all(
        "CCD Identity QC Investigation",
        fields=[
            "name",
            "canary_run",
            "recommendation",
            "identity_decision",
            "identity_group",
            "source_population_status",
        ],
        limit_page_length=100_000,
    )
    qc_rows = [
        row
        for row in all_qc_rows
        if str(row.canary_run or "") in canary_names
        or str(row.recommendation or "") in recommendation_names
        or str(row.identity_decision or "") in decision_names
        or str(row.identity_group or "") in group_names
    ]

    affected_entities = {
        "CCD Match Recommendation": set(recommendation_names),
        "CCD Match Component Review": {str(row.name) for row in component_reviews},
        "CCD Match Evaluation Pair": {str(row.name) for row in evaluation_pairs},
        "CCD Match Review Candidate": {str(row.name) for row in candidates},
        "CCD Match Canary Run": set(canary_names),
        "CCD Match Evaluation Run": set(evaluation_run_names),
        "CCD Match Review Queue Run": set(queue_names),
        "CCD Identity Activation Batch": {str(row.name) for row in activation_batches},
        "CCD Identity Decision": set(decision_names),
        "CCD Identity Group": set(group_names),
        "CCD Identity Membership": set(membership_names),
        "CCD Identity Exclusion": set(exclusion_names),
        "CCD Identity Overlap Resolution": {str(row.name) for row in overlaps},
        "CCD Identity Correction": {str(row.name) for row in corrections},
        "CCD Identity QC Investigation": {str(row.name) for row in qc_rows},
    }
    events = frappe.get_all(
        "CCD Identity Event",
        fields=[
            "name",
            "entity_doctype",
            "entity_name",
            "identity_decision",
            "identity_group",
            "identity_membership",
            "source_population_status",
        ],
        limit_page_length=100_000,
    )
    affected_events = [
        row
        for row in events
        if str(row.identity_decision or "") in decision_names
        or str(row.identity_group or "") in group_names
        or str(row.identity_membership or "") in membership_names
        or str(row.entity_name or "")
        in affected_entities.get(str(row.entity_doctype or ""), set())
    ]

    recommendation_stale = _unique(
        row.name
        for row in recommendations
        if str(row.rollout_state or "") not in {"Applied", "Stale"}
        and str(row.status or "") in {"Proposed", "Approved", "Exception"}
    )
    recommendation_qc_stale = _unique(
        row.name
        for row in recommendations
        if row.qc_selected and not row.qc_stale
    )
    recommendation_qc_status_stale = _unique(
        row.name
        for row in recommendations
        if row.qc_selected
        and str(row.qc_review_status or "") not in FINAL_REVIEW_STATUSES | {"Stale"}
    )
    component_review_stale = _unique(
        row.name for row in component_reviews if not row.stale
    )
    component_review_status_stale = _unique(
        row.name
        for row in component_reviews
        if str(row.review_status or "") not in FINAL_REVIEW_STATUSES | {"Stale"}
    )
    component_materialization_stale = _unique(
        row.name
        for row in component_reviews
        if str(row.materialization_status or "")
        not in HISTORICAL_MATERIALIZATION_STATUSES | {"Stale"}
    )
    candidate_stale = _unique(row.name for row in candidates if not row.stale)
    candidate_status_stale = _unique(
        row.name
        for row in candidates
        if str(row.review_status or "") not in FINAL_REVIEW_STATUSES | {"Stale"}
    )
    candidate_materialization_stale = _unique(
        row.name
        for row in candidates
        if str(row.materialization_status or "")
        not in HISTORICAL_MATERIALIZATION_STATUSES | {"Stale"}
    )
    evaluation_pair_stale = _unique(
        row.name for row in evaluation_pairs if not row.stale
    )
    canary_stale = _unique(
        row.name for row in canary_rows if str(row.status) not in {"Stale", "Superseded"}
    )
    evaluation_run_stale = _unique(
        row.name
        for row in evaluation_runs
        if str(row.status) not in {"Stale", "Superseded"}
    )
    queue_stale = _unique(
        row.name for row in queue_rows if str(row.status) not in {"Stale", "Superseded"}
    )
    activation_batch_stale = _unique(
        row.name
        for row in activation_batches
        if str(row.status) not in {"Applied", "Stale", "Superseded"}
    )
    applied_batches_historical = _unique(
        row.name
        for row in activation_batches
        if str(row.status) == "Applied"
        and str(row.source_population_status or "") != HISTORICAL_SOURCE_RETIRED
    )
    review_batch_stale = _unique(
        row.name
        for row in review_batches
        if str(row.status) not in {"Completed", "Cancelled", "Stale"}
    )

    end_memberships = tuple(group_targets["end_memberships"])
    revalidate_memberships = tuple(group_targets["revalidate_memberships"])
    end_groups = tuple(group_targets["end_groups"])
    revalidate_groups = tuple(group_targets["revalidate_groups"])
    withdrawn_decisions = decision_names
    superseded_exclusions = exclusion_names
    historical_overlaps = _unique(
        row.name
        for row in overlaps
        if str(row.source_population_status or "") != HISTORICAL_SOURCE_RETIRED
    )
    historical_corrections = _unique(
        row.name
        for row in corrections
        if str(row.source_population_status or "") != HISTORICAL_SOURCE_RETIRED
    )
    historical_qc = _unique(
        row.name
        for row in qc_rows
        if str(row.source_population_status or "") != HISTORICAL_SOURCE_RETIRED
    )
    historical_events = _unique(
        row.name
        for row in affected_events
        if str(row.source_population_status or "") != HISTORICAL_SOURCE_RETIRED
    )
    end_unified_memberships = _unique(
        row.name for row in unified_membership_rows
    )

    settings = frappe.get_single(SETTINGS_DOCTYPE)
    settings_authorization_clear = bool(
        settings.automatic_tiered_canary
        or settings.automatic_tiered_policy
        or settings.automatic_tiered_authorization_event
        or settings.last_automatic_batch
    )
    controls_enabled = {
        "materialization_enabled": bool(settings.materialization_enabled),
        "automatic_tiered_enabled": bool(settings.automatic_tiered_enabled),
        "automatic_qc_assignment_enabled": bool(
            settings.automatic_qc_assignment_enabled
        ),
    }

    actions = {
        "recommendation_stale": recommendation_stale,
        "recommendation_qc_stale": recommendation_qc_stale,
        "recommendation_qc_status_stale": recommendation_qc_status_stale,
        "component_review_stale": component_review_stale,
        "component_review_status_stale": component_review_status_stale,
        "component_materialization_stale": component_materialization_stale,
        "candidate_stale": candidate_stale,
        "candidate_status_stale": candidate_status_stale,
        "candidate_materialization_stale": candidate_materialization_stale,
        "evaluation_pair_stale": evaluation_pair_stale,
        "canary_stale": canary_stale,
        "evaluation_run_stale": evaluation_run_stale,
        "queue_stale": queue_stale,
        "activation_batch_stale": activation_batch_stale,
        "review_batch_stale": review_batch_stale,
        "end_memberships": end_memberships,
        "revalidate_memberships": revalidate_memberships,
        "end_groups": end_groups,
        "revalidate_groups": revalidate_groups,
        "withdrawn_decisions": withdrawn_decisions,
        "superseded_exclusions": superseded_exclusions,
        "applied_batches_historical": applied_batches_historical,
        "historical_overlaps": historical_overlaps,
        "historical_corrections": historical_corrections,
        "historical_qc": historical_qc,
        "historical_events": historical_events,
        "end_unified_memberships": end_unified_memberships,
        "settings_authorization_clear": settings_authorization_clear,
    }
    active_keys = (
        "recommendation_stale",
        "recommendation_qc_stale",
        "component_review_stale",
        "candidate_stale",
        "evaluation_pair_stale",
        "canary_stale",
        "evaluation_run_stale",
        "queue_stale",
        "activation_batch_stale",
        "review_batch_stale",
        "end_memberships",
        "revalidate_memberships",
        "end_groups",
        "revalidate_groups",
        "withdrawn_decisions",
        "superseded_exclusions",
        "end_unified_memberships",
    )
    active_counts = {key: len(actions[key]) for key in active_keys}
    historical_counts = {
        "recommendations": len(recommendations),
        "component_reviews": len(component_reviews),
        "evaluation_pairs": len(evaluation_pairs),
        "splink_candidates": len(candidates),
        "canary_runs": len(canary_names),
        "evaluation_runs": len(evaluation_run_names),
        "queue_runs": len(queue_names),
        "activation_batches": len(activation_batches),
        "review_batches": len(review_batches),
        "decisions": len(decisions),
        "groups": len(group_names),
        "memberships": len(group_memberships),
        "exclusions": len(orphan_exclusions),
        "overlap_resolutions": len(overlaps),
        "corrections": len(corrections),
        "qc_investigations": len(qc_rows),
        "events": len(affected_events),
        "unified_person_memberships": len(unified_membership_rows),
        "unified_people": len(
            {str(row.unified_person) for row in unified_membership_rows}
        ),
    }
    missing_ids = {
        str(value)
        for rows in (recommendations, evaluation_pairs, candidates)
        for row in rows
        for value, missing in (
            (row.get("left_record"), row.get("left_missing")),
            (row.get("right_record"), row.get("right_missing")),
        )
        if missing and value
    }
    missing_ids.update(str(row.ccd_master) for row in orphan_memberships)
    missing_ids.update(str(row.ccd_master) for row in unified_membership_rows)
    missing_ids.update(decision_missing | overlap_missing | correction_missing)
    missing_ids.update(retired)
    fingerprint_payload = {
        "version": 2,
        "scope_type": scope_type,
        "scope_metadata": scope_metadata or {},
        "retired_record_ids": sorted(retired),
        "missing_record_ids": sorted(missing_ids),
        "actions": {
            key: value if isinstance(value, bool) else sorted(value)
            for key, value in actions.items()
        },
        "group_survivor_counts": group_targets["survivor_counts"],
    }
    scope_fingerprint = stable_scope_fingerprint(fingerprint_payload)
    public = {
        "zero_write": True,
        "scope_type": scope_type,
        "scope_fingerprint": scope_fingerprint,
        "missing_ccd_master_count": len(missing_ids),
        "target_ccd_master_count": len(retired),
        "active_issue_count": sum(active_counts.values()),
        "active_issue_counts": active_counts,
        "historical_affected_counts": historical_counts,
        "planned_write_count": sum(
            len(value) if not isinstance(value, bool) else int(value)
            for value in actions.values()
        ),
        "controls_enabled": controls_enabled,
        "settings_authorization_will_clear": settings_authorization_clear,
        "sample_affected_documents": {
            key: list(value[:5])
            for key, value in actions.items()
            if not isinstance(value, bool) and value
        },
    }
    return {
        "public": public,
        "actions": actions,
        "group_targets": group_targets,
        "rows": {
            "memberships": {str(row.name): row for row in group_memberships},
            "decisions": {str(row.name): row for row in decisions},
            "exclusions": {str(row.name): row for row in orphan_exclusions},
            "unified_memberships": {
                str(row.name): row for row in unified_membership_rows
            },
        },
        "fingerprint_payload": fingerprint_payload,
    }


def _lock_names(doctype: str, names: Iterable[str]) -> None:
    for chunk in _chunks(names):
        placeholders = ", ".join(["%s"] * len(chunk))
        frappe.db.sql(
            f"SELECT name FROM `tab{doctype}` WHERE name IN ({placeholders}) "
            "ORDER BY name FOR UPDATE",
            chunk,
        )


def _bulk_update(doctype: str, names: Iterable[str], values: dict[str, Any]) -> int:
    total = 0
    assignments = ", ".join(f"`{field}`=%s" for field in values)
    for chunk in _chunks(names):
        placeholders = ", ".join(["%s"] * len(chunk))
        frappe.db.sql(
            f"UPDATE `tab{doctype}` SET {assignments} WHERE name IN ({placeholders})",
            tuple(values.values()) + chunk,
        )
        total += len(chunk)
    return total


def _mark_historical(
    doctype: str, names: Iterable[str], retirement_run: str, retired_at: Any
) -> int:
    return _bulk_update(
        doctype,
        names,
        {
            "source_population_status": HISTORICAL_SOURCE_RETIRED,
            "source_retirement_run": retirement_run,
            "source_retired_at": retired_at,
        },
    )


def _append_retirement_events(
    state: dict[str, Any], retirement_run: str, reason: str
) -> tuple[str, ...]:
    actions = state["actions"]
    membership_rows = state["rows"]["memberships"]
    created: list[str] = []
    for name in actions["end_memberships"]:
        row = membership_rows[name]
        created.append(_append_event(
            entity_doctype="CCD Identity Membership",
            entity_name=name,
            event_type="End",
            reason="historical_source_retired",
            nonce=retirement_run,
            from_status=str(row.status),
            to_status="Ended",
            identity_decision=str(row.originating_decision or ""),
            identity_group=str(row.identity_group or ""),
            identity_membership=name,
            metadata={"retirement_run": retirement_run, "reason": reason},
        ))
    for name in actions["revalidate_memberships"]:
        row = membership_rows[name]
        created.append(_append_event(
            entity_doctype="CCD Identity Membership",
            entity_name=name,
            event_type="Needs Revalidation",
            reason="historical_source_retired",
            nonce=retirement_run,
            from_status=str(row.status),
            to_status="Needs Revalidation",
            identity_decision=str(row.originating_decision or ""),
            identity_group=str(row.identity_group or ""),
            identity_membership=name,
            metadata={"retirement_run": retirement_run, "reason": reason},
        ))
    for name in actions["end_groups"]:
        created.append(_append_event(
            entity_doctype="CCD Identity Group",
            entity_name=name,
            event_type="End",
            reason="fewer_than_two_surviving_members_after_source_retirement",
            nonce=retirement_run,
            from_status="Active",
            to_status="Ended",
            identity_group=name,
            metadata={"retirement_run": retirement_run, "reason": reason},
        ))
    for name in actions["revalidate_groups"]:
        created.append(_append_event(
            entity_doctype="CCD Identity Group",
            entity_name=name,
            event_type="Needs Revalidation",
            reason="member_source_retired",
            nonce=retirement_run,
            from_status="Active",
            to_status="Needs Revalidation",
            identity_group=name,
            metadata={"retirement_run": retirement_run, "reason": reason},
        ))
    for name in actions["withdrawn_decisions"]:
        created.append(_append_event(
            entity_doctype="CCD Identity Decision",
            entity_name=name,
            event_type="Withdraw",
            reason="participant_source_retired",
            nonce=retirement_run,
            from_status="Active",
            to_status="Withdrawn",
            identity_decision=name,
            metadata={"retirement_run": retirement_run, "reason": reason},
        ))
    for name in actions["superseded_exclusions"]:
        row = state["rows"]["exclusions"][name]
        created.append(_append_event(
            entity_doctype="CCD Identity Exclusion",
            entity_name=name,
            event_type="Supersede",
            reason="participant_source_retired",
            nonce=retirement_run,
            from_status="Active",
            to_status="Superseded",
            identity_decision=str(row.originating_decision or ""),
            metadata={"retirement_run": retirement_run, "reason": reason},
        ))
    return tuple(created)


def _lock_state(state: dict[str, Any]) -> None:
    actions = state["actions"]
    lock_map = {
        "CCD Match Recommendation": set(actions["recommendation_stale"])
        | set(actions["recommendation_qc_stale"])
        | set(actions["recommendation_qc_status_stale"]),
        "CCD Match Component Review": set(actions["component_review_stale"])
        | set(actions["component_review_status_stale"])
        | set(actions["component_materialization_stale"]),
        "CCD Match Review Candidate": set(actions["candidate_stale"])
        | set(actions["candidate_status_stale"])
        | set(actions["candidate_materialization_stale"]),
        "CCD Match Evaluation Pair": set(actions["evaluation_pair_stale"]),
        "CCD Match Canary Run": set(actions["canary_stale"]),
        "CCD Match Evaluation Run": set(actions["evaluation_run_stale"]),
        "CCD Match Review Queue Run": set(actions["queue_stale"]),
        "CCD Identity Activation Batch": set(actions["activation_batch_stale"])
        | set(actions["applied_batches_historical"]),
        "CCD Match Review Batch": set(actions["review_batch_stale"]),
        "CCD Identity Membership": set(actions["end_memberships"])
        | set(actions["revalidate_memberships"]),
        "CCD Identity Group": set(actions["end_groups"])
        | set(actions["revalidate_groups"]),
        "CCD Identity Decision": set(actions["withdrawn_decisions"]),
        "CCD Identity Exclusion": set(actions["superseded_exclusions"]),
        "CCD Identity Overlap Resolution": set(actions["historical_overlaps"]),
        "CCD Identity Correction": set(actions["historical_corrections"]),
        "CCD Identity QC Investigation": set(actions["historical_qc"]),
        "CCD Identity Event": set(actions["historical_events"]),
        "CCD Unified Person Membership": set(actions["end_unified_memberships"]),
    }
    for doctype, names in lock_map.items():
        _lock_names(doctype, names)


def _apply_state_actions(
    state: dict[str, Any], retirement_run: str, reason: str, now: Any
) -> None:
    actions = state["actions"]
    _bulk_update(
        "CCD Match Recommendation",
        actions["recommendation_stale"],
        {"rollout_state": "Stale"},
    )
    _bulk_update(
        "CCD Match Recommendation",
        actions["recommendation_qc_stale"],
        {"qc_stale": 1},
    )
    _bulk_update(
        "CCD Match Recommendation",
        actions["recommendation_qc_status_stale"],
        {"qc_review_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Component Review", actions["component_review_stale"], {"stale": 1}
    )
    _bulk_update(
        "CCD Match Component Review",
        actions["component_review_status_stale"],
        {"review_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Component Review",
        actions["component_materialization_stale"],
        {"materialization_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Review Candidate", actions["candidate_stale"], {"stale": 1}
    )
    _bulk_update(
        "CCD Match Review Candidate",
        actions["candidate_status_stale"],
        {"review_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Review Candidate",
        actions["candidate_materialization_stale"],
        {"materialization_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Evaluation Pair", actions["evaluation_pair_stale"], {"stale": 1}
    )
    _bulk_update("CCD Match Canary Run", actions["canary_stale"], {"status": "Stale"})
    _bulk_update(
        "CCD Match Evaluation Run", actions["evaluation_run_stale"], {"status": "Stale"}
    )
    _bulk_update(
        "CCD Match Review Queue Run", actions["queue_stale"], {"status": "Stale"}
    )
    _bulk_update(
        "CCD Identity Activation Batch",
        actions["activation_batch_stale"],
        {"status": "Stale", "error_summary": "historical_source_retired"},
    )
    activation_items = (
        frappe.get_all(
            "CCD Identity Activation Item",
            filters={
                "parent": ["in", actions["activation_batch_stale"]],
                "status": ["not in", ["Applied", "Already Applied", "Corrected"]],
            },
            pluck="name",
            limit_page_length=100_000,
        )
        if actions["activation_batch_stale"]
        else ()
    )
    _lock_names("CCD Identity Activation Item", activation_items)
    _bulk_update(
        "CCD Identity Activation Item",
        activation_items,
        {"status": "Stale", "error_code": "historical_source_retired"},
    )
    _bulk_update(
        "CCD Match Review Batch", actions["review_batch_stale"], {"status": "Stale"}
    )
    review_items = (
        frappe.get_all(
            "CCD Match Review Batch Item",
            filters={
                "parent": ["in", actions["review_batch_stale"]],
                "status": ["!=", "Completed"],
            },
            pluck="name",
            limit_page_length=100_000,
        )
        if actions["review_batch_stale"]
        else ()
    )
    _lock_names("CCD Match Review Batch Item", review_items)
    _bulk_update("CCD Match Review Batch Item", review_items, {"status": "Stale"})
    _bulk_update(
        "CCD Identity Membership",
        actions["end_memberships"],
        {
            "status": "Ended",
            "valid_to": now,
            "ended_reason": "historical_source_retired",
            "ended_by": frappe.session.user,
        },
    )
    _bulk_update(
        "CCD Identity Membership",
        actions["revalidate_memberships"],
        {"status": "Needs Revalidation"},
    )
    _bulk_update(
        "CCD Identity Group",
        actions["end_groups"],
        {"status": "Ended", "active_member_count": 0},
    )
    for survivor_count, group_names in _groups_by_survivor_count(state).items():
        _bulk_update(
            "CCD Identity Group",
            group_names,
            {"status": "Needs Revalidation", "active_member_count": survivor_count},
        )
    _bulk_update(
        "CCD Identity Decision", actions["withdrawn_decisions"], {"status": "Withdrawn"}
    )
    _bulk_update(
        "CCD Identity Exclusion", actions["superseded_exclusions"], {"status": "Superseded"}
    )
    _mark_historical(
        "CCD Identity Activation Batch",
        actions["applied_batches_historical"],
        retirement_run,
        now,
    )
    _mark_historical(
        "CCD Identity Overlap Resolution",
        actions["historical_overlaps"],
        retirement_run,
        now,
    )
    _mark_historical(
        "CCD Identity Correction",
        actions["historical_corrections"],
        retirement_run,
        now,
    )
    _mark_historical(
        "CCD Identity QC Investigation",
        actions["historical_qc"],
        retirement_run,
        now,
    )
    _mark_historical(
        "CCD Identity Event", actions["historical_events"], retirement_run, now
    )
    frappe.db.set_value(
        SETTINGS_DOCTYPE,
        SETTINGS_DOCTYPE,
        {
            "materialization_enabled": 0,
            "automatic_tiered_enabled": 0,
            "automatic_qc_assignment_enabled": 0,
            "automatic_tiered_canary": None,
            "automatic_tiered_policy": None,
            "automatic_tiered_authorization_event": None,
            "last_automatic_batch": None,
            "last_automatic_status": "Disabled - historical source retired",
            "last_automatic_error": None,
        },
        update_modified=False,
    )
    new_events = _append_retirement_events(state, retirement_run, reason)
    _mark_historical("CCD Identity Event", new_events, retirement_run, now)
    if actions["end_unified_memberships"]:
        from db_connector.api_unified_person import retire_unified_person_records

        retire_unified_person_records(
            [
                state["rows"]["unified_memberships"][name].ccd_master
                for name in actions["end_unified_memberships"]
            ],
            reason="historical_source_retired",
            origin_doctype=RETIREMENT_DOCTYPE,
            origin_document=retirement_run,
        )


@frappe.whitelist()
def preview_orphan_retirement() -> dict[str, Any]:
    """Return a frozen, zero-write preview of all active orphan state."""
    _require_manager()
    return _collect_state()["public"]


@frappe.whitelist()
def apply_orphan_retirement(
    confirm_scope_fingerprint: str, reason: str
) -> dict[str, Any]:
    """Atomically retire orphaned state after exact preview confirmation."""
    _require_manager()
    reason = str(reason or "").strip()
    fingerprint = str(confirm_scope_fingerprint or "").strip()
    if not reason:
        frappe.throw("A retirement reason is required")
    if len(fingerprint) != 64:
        frappe.throw("Type the exact 64-character preview fingerprint to confirm")

    retirement_key = hashlib.sha256(f"orphan-repair\x1f{fingerprint}".encode()).hexdigest()
    existing_run = frappe.db.get_value(
        RETIREMENT_DOCTYPE,
        {"retirement_key": retirement_key, "status": "Applied"},
        "name",
    )
    if existing_run:
        return {
            "retirement_run": str(existing_run),
            "status": "Applied",
            "idempotent": True,
        }

    frappe.db.sql(
        "SELECT value FROM `tabSingles` WHERE doctype=%s ORDER BY field FOR UPDATE",
        (SETTINGS_DOCTYPE,),
    )
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    enabled = {
        "materialization_enabled": bool(settings.materialization_enabled),
        "automatic_tiered_enabled": bool(settings.automatic_tiered_enabled),
        "automatic_qc_assignment_enabled": bool(
            settings.automatic_qc_assignment_enabled
        ),
    }
    if any(enabled.values()):
        frappe.throw(
            "Materialization, Automatic Tiered, and Automatic QC must all be disabled"
        )

    state = _collect_state()
    if state["public"]["scope_fingerprint"] != fingerprint:
        frappe.throw("The orphan population changed; run a fresh zero-write preview")
    if not state["public"]["planned_write_count"]:
        return {
            **state["public"],
            "status": "No Changes",
            "idempotent": True,
        }

    actions = state["actions"]
    lock_map = {
        "CCD Match Recommendation": set(actions["recommendation_stale"])
        | set(actions["recommendation_qc_stale"])
        | set(actions["recommendation_qc_status_stale"]),
        "CCD Match Component Review": set(actions["component_review_stale"])
        | set(actions["component_review_status_stale"])
        | set(actions["component_materialization_stale"]),
        "CCD Match Review Candidate": set(actions["candidate_stale"])
        | set(actions["candidate_status_stale"])
        | set(actions["candidate_materialization_stale"]),
        "CCD Match Evaluation Pair": set(actions["evaluation_pair_stale"]),
        "CCD Match Canary Run": set(actions["canary_stale"]),
        "CCD Match Evaluation Run": set(actions["evaluation_run_stale"]),
        "CCD Match Review Queue Run": set(actions["queue_stale"]),
        "CCD Identity Activation Batch": set(actions["activation_batch_stale"])
        | set(actions["applied_batches_historical"]),
        "CCD Match Review Batch": set(actions["review_batch_stale"]),
        "CCD Identity Membership": set(actions["end_memberships"])
        | set(actions["revalidate_memberships"]),
        "CCD Identity Group": set(actions["end_groups"])
        | set(actions["revalidate_groups"]),
        "CCD Identity Decision": set(actions["withdrawn_decisions"]),
        "CCD Identity Exclusion": set(actions["superseded_exclusions"]),
        "CCD Identity Overlap Resolution": set(actions["historical_overlaps"]),
        "CCD Identity Correction": set(actions["historical_corrections"]),
        "CCD Identity QC Investigation": set(actions["historical_qc"]),
        "CCD Identity Event": set(actions["historical_events"]),
        "CCD Unified Person Membership": set(actions["end_unified_memberships"]),
    }
    for doctype, names in lock_map.items():
        _lock_names(doctype, names)

    now = frappe.utils.now_datetime()
    run = frappe.get_doc(
        {
            "doctype": RETIREMENT_DOCTYPE,
            "retirement_key": retirement_key,
            "scope_type": "Orphan Repair",
            "scope_fingerprint": fingerprint,
            "status": "Applying",
            "reason": reason,
            "preview_json": canonical_json(state["public"]),
            "missing_record_count": state["public"]["missing_ccd_master_count"],
            "active_issue_count": state["public"]["active_issue_count"],
            "started_at": now,
            "started_by": frappe.session.user,
        }
    ).insert(ignore_permissions=True)

    _bulk_update(
        "CCD Match Recommendation",
        actions["recommendation_stale"],
        {"rollout_state": "Stale"},
    )
    _bulk_update(
        "CCD Match Recommendation",
        actions["recommendation_qc_stale"],
        {"qc_stale": 1},
    )
    _bulk_update(
        "CCD Match Recommendation",
        actions["recommendation_qc_status_stale"],
        {"qc_review_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Component Review", actions["component_review_stale"], {"stale": 1}
    )
    _bulk_update(
        "CCD Match Component Review",
        actions["component_review_status_stale"],
        {"review_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Component Review",
        actions["component_materialization_stale"],
        {"materialization_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Review Candidate", actions["candidate_stale"], {"stale": 1}
    )
    _bulk_update(
        "CCD Match Review Candidate",
        actions["candidate_status_stale"],
        {"review_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Review Candidate",
        actions["candidate_materialization_stale"],
        {"materialization_status": "Stale"},
    )
    _bulk_update(
        "CCD Match Evaluation Pair", actions["evaluation_pair_stale"], {"stale": 1}
    )
    _bulk_update("CCD Match Canary Run", actions["canary_stale"], {"status": "Stale"})
    _bulk_update(
        "CCD Match Evaluation Run", actions["evaluation_run_stale"], {"status": "Stale"}
    )
    _bulk_update(
        "CCD Match Review Queue Run", actions["queue_stale"], {"status": "Stale"}
    )
    _bulk_update(
        "CCD Identity Activation Batch",
        actions["activation_batch_stale"],
        {"status": "Stale", "error_summary": "historical_source_retired"},
    )
    _bulk_update(
        "CCD Identity Activation Item",
        frappe.get_all(
            "CCD Identity Activation Item",
            filters={
                "parent": ["in", actions["activation_batch_stale"]],
                "status": ["not in", ["Applied", "Already Applied", "Corrected"]],
            },
            pluck="name",
            limit_page_length=100_000,
        ) if actions["activation_batch_stale"] else (),
        {"status": "Stale", "error_code": "historical_source_retired"},
    )
    _bulk_update(
        "CCD Match Review Batch", actions["review_batch_stale"], {"status": "Stale"}
    )
    _bulk_update(
        "CCD Match Review Batch Item",
        frappe.get_all(
            "CCD Match Review Batch Item",
            filters={
                "parent": ["in", actions["review_batch_stale"]],
                "status": ["!=", "Completed"],
            },
            pluck="name",
            limit_page_length=100_000,
        ) if actions["review_batch_stale"] else (),
        {"status": "Stale"},
    )
    _bulk_update(
        "CCD Identity Membership",
        actions["end_memberships"],
        {
            "status": "Ended",
            "valid_to": now,
            "ended_reason": "historical_source_retired",
            "ended_by": frappe.session.user,
        },
    )
    _bulk_update(
        "CCD Identity Membership",
        actions["revalidate_memberships"],
        {"status": "Needs Revalidation"},
    )
    _bulk_update(
        "CCD Identity Group",
        actions["end_groups"],
        {"status": "Ended", "active_member_count": 0},
    )
    for survivor_count, group_names in _groups_by_survivor_count(state).items():
        _bulk_update(
            "CCD Identity Group",
            group_names,
            {"status": "Needs Revalidation", "active_member_count": survivor_count},
        )
    _bulk_update(
        "CCD Identity Decision", actions["withdrawn_decisions"], {"status": "Withdrawn"}
    )
    _bulk_update(
        "CCD Identity Exclusion", actions["superseded_exclusions"], {"status": "Superseded"}
    )

    _mark_historical(
        "CCD Identity Activation Batch", actions["applied_batches_historical"], run.name, now
    )
    _mark_historical(
        "CCD Identity Overlap Resolution", actions["historical_overlaps"], run.name, now
    )
    _mark_historical(
        "CCD Identity Correction", actions["historical_corrections"], run.name, now
    )
    _mark_historical(
        "CCD Identity QC Investigation", actions["historical_qc"], run.name, now
    )
    _mark_historical(
        "CCD Identity Event", actions["historical_events"], run.name, now
    )

    frappe.db.set_value(
        SETTINGS_DOCTYPE,
        settings.name,
        {
            "materialization_enabled": 0,
            "automatic_tiered_enabled": 0,
            "automatic_qc_assignment_enabled": 0,
            "automatic_tiered_canary": None,
            "automatic_tiered_policy": None,
            "automatic_tiered_authorization_event": None,
            "last_automatic_batch": None,
            "last_automatic_status": "Disabled - historical source retired",
            "last_automatic_error": None,
        },
        update_modified=False,
    )
    new_events = _append_retirement_events(state, run.name, reason)
    _mark_historical("CCD Identity Event", new_events, run.name, now)
    if actions["end_unified_memberships"]:
        from db_connector.api_unified_person import retire_unified_person_records

        retire_unified_person_records(
            [
                state["rows"]["unified_memberships"][name].ccd_master
                for name in actions["end_unified_memberships"]
            ],
            reason="historical_source_retired",
            origin_doctype=RETIREMENT_DOCTYPE,
            origin_document=run.name,
        )

    result = {
        "retirement_run": run.name,
        "status": "Applied",
        "scope_fingerprint": fingerprint,
        "counts": state["public"]["active_issue_counts"],
        "historical_affected_counts": state["public"]["historical_affected_counts"],
    }
    frappe.db.set_value(
        RETIREMENT_DOCTYPE,
        run.name,
        {
            "status": "Applied",
            "result_json": canonical_json(result),
            "completed_at": frappe.utils.now_datetime(),
            "completed_by": frappe.session.user,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return result


def _groups_by_survivor_count(state: dict[str, Any]) -> dict[int, tuple[str, ...]]:
    output: dict[int, list[str]] = defaultdict(list)
    target_names = set(state["actions"]["revalidate_groups"])
    for group_name, count in state["group_targets"]["survivor_counts"].items():
        if group_name in target_names:
            output[int(count)].append(group_name)
    return {count: tuple(sorted(names)) for count, names in output.items()}


def stable_source_key(ccd_reg_doctype: str) -> str:
    """Return the immutable source key encoded by a generated CCD DocType."""
    value = str(ccd_reg_doctype or "").strip()
    if value.startswith("CCD-REG-"):
        value = value[len("CCD-REG-") :]
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        frappe.throw("CCD Registration has no valid stable source key")
    return value


def registration_source_key(registration: Any) -> str:
    return stable_source_key(str(registration.get("ccd_reg_doctype") or ""))


def resolve_source_key(value: str) -> str:
    """Resolve a Registration revision name or generated DocType to its key."""
    candidate = str(value or "").strip()
    if frappe.db.exists("CCD Registration", candidate):
        return registration_source_key(
            frappe.get_doc("CCD Registration", candidate)
        )
    return stable_source_key(candidate)


def _source_keys(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            frappe.throw("source_keys must be a JSON array")
    if not isinstance(value, (list, tuple)):
        frappe.throw("source_keys must be an array")
    output = tuple(sorted({str(item) for item in value if str(item)}))
    if len(output) > 100_000:
        frappe.throw("A single confirmed deletion is limited to 100,000 source keys")
    return output


def _source_record_ids(
    source_name: str, source_keys: tuple[str, ...] | None
) -> tuple[str, ...]:
    source = str(source_name or "").strip()
    if not source:
        frappe.throw("A stable CCD source key is required")
    names: set[str] = set()
    if source_keys is None:
        names.update(
            str(value)
            for value in frappe.get_all(
                MASTER_DOCTYPE,
                filters={"ccd_reg_source": source},
                pluck="name",
                limit_page_length=1_000_000,
            )
        )
    else:
        for chunk in _chunks(source_keys):
            names.update(
                str(value)
                for value in frappe.get_all(
                    MASTER_DOCTYPE,
                    filters={
                        "ccd_reg_source": source,
                        "ccd_source_key": ["in", chunk],
                    },
                    pluck="name",
                    limit_page_length=100_000,
                )
            )
    return tuple(sorted(names))


def _empty_source_preview(
    source_name: str, source_keys: tuple[str, ...] | None
) -> dict[str, Any]:
    metadata = {
        "source_name": source_name,
        "selection": "all" if source_keys is None else "source_keys",
        "source_key_count": 0 if source_keys is None else len(source_keys),
        "source_keys_sha256": (
            ""
            if source_keys is None
            else hashlib.sha256(canonical_json(source_keys).encode()).hexdigest()
        ),
    }
    fingerprint = stable_scope_fingerprint(
        {
            "version": 2,
            "scope_type": "Source Retirement",
            "scope_metadata": metadata,
            "retired_record_ids": [],
        }
    )
    return {
        "zero_write": True,
        "scope_type": "Source Retirement",
        "scope_fingerprint": fingerprint,
        "source_name": source_name,
        "target_ccd_master_count": 0,
        "missing_ccd_master_count": 0,
        "active_issue_count": 0,
        "active_issue_counts": {},
        "historical_affected_counts": {},
        "planned_write_count": 0,
        "controls_enabled": _control_state(),
        "settings_authorization_will_clear": False,
        "sample_affected_documents": {},
    }


def _control_state() -> dict[str, bool]:
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    return {
        "materialization_enabled": bool(settings.materialization_enabled),
        "automatic_tiered_enabled": bool(settings.automatic_tiered_enabled),
        "automatic_qc_assignment_enabled": bool(
            settings.automatic_qc_assignment_enabled
        ),
    }


def _source_state(
    source_name: str, source_keys: tuple[str, ...] | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    source = resolve_source_key(source_name)
    target_ids = _source_record_ids(source, source_keys)
    if not target_ids:
        return None, _empty_source_preview(source, source_keys)
    metadata = {
        "source_name": source,
        "selection": "all" if source_keys is None else "source_keys",
        "source_key_count": 0 if source_keys is None else len(source_keys),
        "source_keys_sha256": (
            ""
            if source_keys is None
            else hashlib.sha256(canonical_json(source_keys).encode()).hexdigest()
        ),
    }
    state = _collect_state(
        target_ids,
        scope_type="Source Retirement",
        scope_metadata=metadata,
    )
    state["public"]["source_name"] = source
    return state, state["public"]


@frappe.whitelist()
def preview_source_retirement(
    source_name: str, source_keys: Any = None
) -> dict[str, Any]:
    """Preview a source-scoped CCD Master deletion without persistent writes."""
    _require_manager()
    _state, public = _source_state(source_name, _source_keys(source_keys))
    return public


def _apply_source_retirement(
    source_name: str,
    source_keys: tuple[str, ...] | None,
    fingerprint: str,
    reason: str,
    *,
    commit: bool,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    fingerprint = str(fingerprint or "").strip()
    if not reason:
        frappe.throw("A retirement reason is required")
    if len(fingerprint) != 64:
        frappe.throw("Type the exact 64-character preview fingerprint to confirm")
    retirement_key = hashlib.sha256(
        f"source-retirement\x1f{fingerprint}".encode()
    ).hexdigest()
    existing_run = frappe.db.get_value(
        RETIREMENT_DOCTYPE,
        {"retirement_key": retirement_key, "status": "Applied"},
        "name",
    )
    if existing_run:
        return {
            "retirement_run": str(existing_run),
            "status": "Applied",
            "idempotent": True,
        }

    frappe.db.sql(
        "SELECT value FROM `tabSingles` WHERE doctype=%s ORDER BY field FOR UPDATE",
        (SETTINGS_DOCTYPE,),
    )
    enabled = _control_state()
    if any(enabled.values()):
        frappe.throw(
            "Materialization, Automatic Tiered, and Automatic QC must all be disabled"
        )

    source_name = resolve_source_key(source_name)
    target_ids = _source_record_ids(source_name, source_keys)
    _lock_names(MASTER_DOCTYPE, target_ids)
    state, public = _source_state(source_name, source_keys)
    if public["scope_fingerprint"] != fingerprint:
        frappe.throw("The deletion population changed; run a fresh zero-write preview")
    if not target_ids:
        return {**public, "status": "No Changes", "idempotent": True}
    if state is None:
        frappe.throw("Unable to reconstruct the confirmed source retirement scope")

    _lock_state(state)
    now = frappe.utils.now_datetime()
    run = frappe.get_doc(
        {
            "doctype": RETIREMENT_DOCTYPE,
            "retirement_key": retirement_key,
            "scope_type": "Source Retirement",
            "scope_fingerprint": fingerprint,
            "status": "Applying",
            "reason": reason,
            "preview_json": canonical_json(public),
            "missing_record_count": len(target_ids),
            "active_issue_count": public["active_issue_count"],
            "started_at": now,
            "started_by": frappe.session.user,
        }
    ).insert(ignore_permissions=True)
    _apply_state_actions(state, run.name, reason, now)
    deleted = 0
    for chunk in _chunks(target_ids):
        placeholders = ", ".join(["%s"] * len(chunk))
        frappe.db.sql(
            f"DELETE FROM `tab{MASTER_DOCTYPE}` WHERE name IN ({placeholders})",
            chunk,
        )
        deleted += len(chunk)

    result = {
        "retirement_run": run.name,
        "status": "Applied",
        "scope_fingerprint": fingerprint,
        "source_name": str(source_name),
        "deleted_ccd_masters": deleted,
        "counts": public["active_issue_counts"],
        "historical_affected_counts": public["historical_affected_counts"],
    }
    frappe.db.set_value(
        RETIREMENT_DOCTYPE,
        run.name,
        {
            "status": "Applied",
            "result_json": canonical_json(result),
            "completed_at": frappe.utils.now_datetime(),
            "completed_by": frappe.session.user,
        },
        update_modified=False,
    )
    if commit:
        frappe.db.commit()
    return result


@frappe.whitelist()
def apply_source_retirement(
    source_name: str,
    confirm_scope_fingerprint: str,
    reason: str,
    source_keys: Any = None,
) -> dict[str, Any]:
    """Retire identity state, then delete the exact confirmed CCD Master scope."""
    _require_manager()
    return _apply_source_retirement(
        source_name,
        _source_keys(source_keys),
        confirm_scope_fingerprint,
        reason,
        commit=True,
    )


def _validated_generated_doctype(doctype: str) -> str:
    value = str(doctype or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        frappe.throw("Invalid generated CCD DocType")
    metadata = frappe.db.get_value(
        "DocType", value, ["custom", "module"], as_dict=True
    )
    referenced = bool(
        frappe.db.exists("CCD Registration", {"ccd_reg_doctype": value})
    )
    if not metadata or not (
        value.startswith("CCD-REG-")
        or (metadata.custom and metadata.module == "hksr-ccd" and referenced)
    ):
        frappe.throw("The DocType is not governed CCD staging data")
    return value


def _staging_record_names(
    doctype: str, source_keys: tuple[str, ...] | None
) -> tuple[str, ...]:
    name = _validated_generated_doctype(doctype)
    if source_keys is None:
        return tuple(
            sorted(
                str(value)
                for value in frappe.get_all(
                    name, pluck="name", limit_page_length=1_000_000
                )
            )
        )
    if not frappe.get_meta(name).has_field("ccd_source_key"):
        frappe.throw(f"{name} has no ccd_source_key field")
    records: set[str] = set()
    for chunk in _chunks(source_keys):
        records.update(
            str(value)
            for value in frappe.get_all(
                name,
                filters={"ccd_source_key": ["in", chunk]},
                pluck="name",
                limit_page_length=100_000,
            )
        )
    return tuple(sorted(records))


def _staging_preview(
    doctype: str, source_keys: tuple[str, ...] | None
) -> dict[str, Any]:
    name = _validated_generated_doctype(doctype)
    records = _staging_record_names(name, source_keys)
    payload = {
        "version": 1,
        "scope_type": "Source Retirement",
        "target_doctype": name,
        "selection": "all" if source_keys is None else "source_keys",
        "source_keys": [] if source_keys is None else list(source_keys),
        "record_names": list(records),
    }
    return {
        "zero_write": True,
        "scope_type": "Source Retirement",
        "scope_fingerprint": stable_scope_fingerprint(payload),
        "target_doctype": name,
        "target_record_count": len(records),
        "planned_write_count": len(records),
        "controls_enabled": _control_state(),
    }


@frappe.whitelist()
def preview_bulk_deletion(
    doctype: str,
    source_name: str | None = None,
    source_keys: Any = None,
) -> dict[str, Any]:
    """One preview entry point for all governed bulk-deletion APIs."""
    _require_manager()
    keys = _source_keys(source_keys)
    if str(doctype) == MASTER_DOCTYPE:
        if not source_name:
            frappe.throw("source_name is required for CCD Master deletion")
        _state, public = _source_state(str(source_name), keys)
        return public
    return _staging_preview(doctype, keys)


def _apply_staging_deletion(
    doctype: str,
    source_keys: tuple[str, ...] | None,
    fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    fingerprint = str(fingerprint or "").strip()
    if not reason:
        frappe.throw("A deletion reason is required")
    if len(fingerprint) != 64:
        frappe.throw("Type the exact 64-character preview fingerprint to confirm")
    retirement_key = hashlib.sha256(
        f"staging-deletion\x1f{fingerprint}".encode()
    ).hexdigest()
    existing_run = frappe.db.get_value(
        RETIREMENT_DOCTYPE,
        {"retirement_key": retirement_key, "status": "Applied"},
        "name",
    )
    if existing_run:
        return {
            "retirement_run": str(existing_run),
            "status": "Applied",
            "idempotent": True,
        }
    preview = _staging_preview(doctype, source_keys)
    if preview["scope_fingerprint"] != fingerprint:
        frappe.throw("The deletion population changed; run a fresh zero-write preview")
    names = _staging_record_names(doctype, source_keys)
    _lock_names(doctype, names)
    locked_preview = _staging_preview(doctype, source_keys)
    if locked_preview["scope_fingerprint"] != fingerprint:
        frappe.throw("The deletion population changed while acquiring locks")
    if not names:
        return {**preview, "status": "No Changes", "idempotent": True}
    now = frappe.utils.now_datetime()
    run = frappe.get_doc(
        {
            "doctype": RETIREMENT_DOCTYPE,
            "retirement_key": retirement_key,
            "scope_type": "Source Retirement",
            "scope_fingerprint": fingerprint,
            "status": "Applying",
            "reason": reason,
            "preview_json": canonical_json(preview),
            "missing_record_count": 0,
            "active_issue_count": 0,
            "started_at": now,
            "started_by": frappe.session.user,
        }
    ).insert(ignore_permissions=True)
    deleted = 0
    for chunk in _chunks(names):
        placeholders = ", ".join(["%s"] * len(chunk))
        frappe.db.sql(
            f"DELETE FROM `tab{_validated_generated_doctype(doctype)}` "
            f"WHERE name IN ({placeholders})",
            chunk,
        )
        deleted += len(chunk)
    result = {
        "retirement_run": run.name,
        "status": "Applied",
        "scope_fingerprint": fingerprint,
        "target_doctype": str(doctype),
        "deleted_records": deleted,
    }
    frappe.db.set_value(
        RETIREMENT_DOCTYPE,
        run.name,
        {
            "status": "Applied",
            "result_json": canonical_json(result),
            "completed_at": frappe.utils.now_datetime(),
            "completed_by": frappe.session.user,
        },
        update_modified=False,
    )
    frappe.db.commit()
    return result


@frappe.whitelist()
def apply_bulk_deletion(
    doctype: str,
    confirm_scope_fingerprint: str,
    reason: str,
    source_name: str | None = None,
    source_keys: Any = None,
) -> dict[str, Any]:
    """Apply the exact scope returned by :func:`preview_bulk_deletion`."""
    _require_manager()
    keys = _source_keys(source_keys)
    if str(doctype) == MASTER_DOCTYPE:
        if not source_name:
            frappe.throw("source_name is required for CCD Master deletion")
        return _apply_source_retirement(
            str(source_name),
            keys,
            confirm_scope_fingerprint,
            reason,
            commit=True,
        )
    return _apply_staging_deletion(
        doctype, keys, confirm_scope_fingerprint, reason
    )


@frappe.whitelist()
def preview_registration_cancellation(registration_name: str) -> dict[str, Any]:
    """Preview the CCD Master and identity impact of cancelling a revision."""
    _require_manager()
    registration = frappe.get_doc("CCD Registration", registration_name)
    if int(registration.docstatus or 0) != 1:
        frappe.throw("Only a submitted CCD Registration can be cancelled")
    source = registration_source_key(registration)
    _state, public = _source_state(source, None)
    return {
        **public,
        "registration": registration.name,
        "stable_source_key": source,
        "generated_doctype": str(registration.get("ccd_reg_doctype") or ""),
    }


@frappe.whitelist()
def cancel_registration_with_retirement(
    registration_name: str,
    confirm_scope_fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    """Cancel a Registration and retire/delete its source in one transaction."""
    _require_manager()
    registration = frappe.get_doc("CCD Registration", registration_name)
    if int(registration.docstatus or 0) != 1:
        frappe.throw("Only a submitted CCD Registration can be cancelled")
    confirmation = {
        "registration": str(registration.name),
        "scope_fingerprint": str(confirm_scope_fingerprint or ""),
        "reason": str(reason or "").strip(),
    }
    frappe.flags.ccd_registration_retirement_confirmation = confirmation
    try:
        registration.cancel()
        result = dict(registration.flags.get("identity_retirement_result") or {})
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.flags.ccd_registration_retirement_confirmation = None
    return {
        **result,
        "registration": registration.name,
        "registration_status": "Cancelled",
    }


def before_cancel_registration(registration: Any, method: str | None = None) -> None:
    """Fail closed unless cancellation came through the confirmed service."""
    _require_manager()
    confirmation = getattr(
        frappe.flags, "ccd_registration_retirement_confirmation", None
    )
    if not confirmation or str(confirmation.get("registration")) != str(
        registration.name
    ):
        frappe.throw(
            "Use Cancel with Identity Retirement: preview the impact and type "
            "the exact scope fingerprint before cancelling."
        )
    source = registration_source_key(registration)
    result = _apply_source_retirement(
        source,
        None,
        str(confirmation.get("scope_fingerprint") or ""),
        str(confirmation.get("reason") or ""),
        commit=False,
    )
    registration.flags.identity_retirement_result = result


def on_cancel_registration(registration: Any, method: str | None = None) -> None:
    """Remove the generated staging DocType after governed source retirement."""
    target_doctype = str(registration.get("ccd_reg_doctype") or "").strip()
    if not target_doctype or not frappe.db.exists("DocType", target_doctype):
        return
    from db_connector.api_ccd import (
        drop_generated_table,
        remove_from_workspace,
        remove_shortcut_from_workspace,
    )

    drop_generated_table(target_doctype)
    remove_from_workspace(target_doctype)
    workspace_label = str(registration.get("sc_label") or registration.name)
    unique_label = f"{workspace_label} ({registration.name})"
    for label in {workspace_label, unique_label}:
        remove_shortcut_from_workspace(
            "Common Client Database",
            f"{label} web page",
            str(registration.get("url") or "") or None,
        )
    frappe.delete_doc(
        "DocType", target_doctype, ignore_permissions=True, force=True
    )


def validate_registration_source_key(
    registration: Any, method: str | None = None
) -> None:
    """Persist and protect the source key encoded by ccd_reg_doctype."""
    source = registration_source_key(registration)
    if not frappe.get_meta("CCD Registration").has_field("ccd_stable_source_key"):
        return
    existing = None
    if not registration.is_new():
        existing = frappe.db.get_value(
            "CCD Registration", registration.name, "ccd_stable_source_key"
        )
    if existing and str(existing) != source:
        frappe.throw("The stable CCD source key is immutable")
    registration.ccd_stable_source_key = source


def before_submit_registration(
    registration: Any, method: str | None = None
) -> None:
    """Allow only one submitted revision for each stable source key."""
    validate_registration_source_key(registration)
    source = registration_source_key(registration)
    conflicts = frappe.get_all(
        "CCD Registration",
        filters={
            "name": ["!=", registration.name],
            "docstatus": 1,
            "ccd_stable_source_key": source,
        },
        pluck="name",
        limit_page_length=2,
    )
    if conflicts:
        frappe.throw(
            f"Submitted CCD Registration revision {conflicts[0]} already owns "
            f"stable source key {source}; cancel it through governed retirement first"
        )


def get_orphan_integrity_audit() -> dict[str, Any]:
    """System Manager entry point used by the manual integrity report."""
    _require_manager()
    return _collect_state()["public"]


def run_scheduled_orphan_integrity_audit() -> dict[str, Any]:
    """Daily read-only audit; record an Error Log only when active issues exist."""
    state = _collect_state()["public"]
    if state["active_issue_count"]:
        frappe.log_error(
            title="CCD identity orphan integrity alert",
            message=canonical_json(state),
        )
    return state
