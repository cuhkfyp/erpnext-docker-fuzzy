"""Development-only isolated fixtures for QC and Tiered automation acceptance.

The fixture creates six independent deterministic-High components in one
pair-scoped canary. It never creates Identity Decisions, Groups, Memberships,
or Exclusions. Live materialization and Automatic Tiered must both be disabled
before creation; later writes happen only through the acceptance steps.
"""

from __future__ import annotations

import json
from typing import Any

import frappe

from db_connector.api_fuzzy_canary import (
    APPROVED_HIGH_REASON,
    RECOMMENDATION_DOCTYPE,
    RUN_DOCTYPE,
    _append_event,
    _canary_prerequisites,
    _initialize_review_workflow,
    _json,
    _pair_fingerprint,
    _recommendation_key,
    _refresh_run_counts,
    _trusted_id_metadata,
)
from db_connector.api_fuzzy_evaluation import (
    DEFAULT_PILOT_POLICY_VERSION,
    _canonical_record,
)
from db_connector.api_identity_resolution import materialization_enabled
from db_connector.fuzzy_matching.blocking import (
    BLOCKING_VERSION,
    generate_candidate_pairs,
)
from db_connector.fuzzy_matching.canary import CanaryEdge, analyze_canary_edges
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.models import build_evidence, tiered_result
from db_connector.fuzzy_matching.types import MatchTier
from db_connector.synthetic_overlap_fixture import (
    SOURCES,
    _ensure_master_record,
    _ensure_raw_record,
)


FIXTURE_ID = "synthetic-qc-automation-v1-20260828"
CONFIRMATION = "CREATE DEVELOPMENT SYNTHETIC QC AUTOMATION FIXTURE"
OVERDUE_CONFIRMATION = "MAKE DEVELOPMENT SYNTHETIC QC CASE OVERDUE"
CADENCE_CONFIRMATION = "MAKE DEVELOPMENT SYNTHETIC QC CADENCE DUE"
SETTINGS_DOCTYPE = "CCD Identity Resolution Settings"
PAIR_COUNT = 6
INITIAL_QC_COUNT = 3


def _person(pair_number: int, side: str, source_key: str) -> dict[str, str]:
    source = SOURCES[source_key]
    pair_code = f"P{pair_number:02d}"
    return {
        "fixture_record": f"{pair_code}_{side}",
        "ccd_source_key": f"SYNTH-QCA-20260828-{pair_code}-{side}",
        "source": str(source["source"]),
        "raw_doctype": str(source["raw_doctype"]),
        "chi_surname": f"測{pair_number}",
        "chi_firstname": f"自動{pair_number}",
        "eng_surname": f"SYNTHQCA{pair_number}",
        "eng_firstname": f"AUTOMATION {pair_number}",
        "phone_num": f"9918{pair_number:04d}",
        "email": f"synthetic-qca-{pair_number}@example.invalid",
    }


PEOPLE = {
    f"P{pair_number:02d}_{side}": _person(
        pair_number, side, "DHCE" if side == "L" else "HMSSHP"
    )
    for pair_number in range(1, PAIR_COUNT + 1)
    for side in ("L", "R")
}

PAIR_LABELS = tuple(
    (f"P{pair_number:02d}_L", f"P{pair_number:02d}_R")
    for pair_number in range(1, PAIR_COUNT + 1)
)


def _fixture_label() -> str:
    return f"DEVELOPMENT_ONLY_SYNTHETIC:{FIXTURE_ID}"


def _existing_run() -> str:
    return str(
        frappe.db.get_value(RUN_DOCTYPE, {"error_summary": _fixture_label()}, "name")
        or ""
    )


def _master_manifest() -> tuple[dict[str, str], dict[str, str]]:
    masters: dict[str, str] = {}
    raw_records: dict[str, str] = {}
    for label, person in PEOPLE.items():
        masters[label] = str(
            frappe.db.get_value(
                "CCD Master",
                {
                    "ccd_reg_source": person["source"],
                    "ccd_source_key": person["ccd_source_key"],
                },
                "name",
            )
            or ""
        )
        raw_records[label] = str(
            frappe.db.get_value(
                person["raw_doctype"],
                {"ccd_source_key": person["ccd_source_key"]},
                "name",
            )
            or ""
        )
    return masters, raw_records


def inspect_synthetic_qc_automation_fixture() -> dict[str, Any]:
    """Return the fixture manifest without changing any data."""
    masters, raw_records = _master_manifest()
    run_name = _existing_run()
    recommendations = []
    if run_name:
        recommendations = [
            dict(row)
            for row in frappe.get_all(
                RECOMMENDATION_DOCTYPE,
                filters={"canary_run": run_name},
                fields=[
                    "name",
                    "left_record",
                    "right_record",
                    "status",
                    "rollout_state",
                    "qc_selected",
                    "qc_assigned_at",
                    "qc_review_status",
                    "qc_final_label",
                ],
                order_by="name",
                limit_page_length=PAIR_COUNT + 1,
            )
        ]
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    return {
        "fixture_id": FIXTURE_ID,
        "development_only": True,
        "materialization_enabled": bool(materialization_enabled(automated=False)),
        "automatic_tiered_enabled": bool(settings.automatic_tiered_enabled),
        "automatic_qc_assignment_enabled": bool(
            settings.automatic_qc_assignment_enabled
        ),
        "run": run_name,
        "masters": masters,
        "raw_records": raw_records,
        "recommendations": recommendations,
    }


def _build_edges(
    masters: dict[str, str], policy: Any
) -> tuple[dict[str, dict[str, Any]], list[CanaryEdge]]:
    records = {
        label: _canonical_record(
            dict(frappe.get_doc("CCD Master", masters[label]).as_dict()), policy
        )
        for label in PEOPLE
    }
    by_id = {str(record["record_id"]): record for record in records.values()}
    blocked = generate_candidate_pairs(list(records.values()), policy)
    expected_pairs = {
        frozenset((masters[left], masters[right])) for left, right in PAIR_LABELS
    }
    actual_pairs = {
        frozenset((str(pair.left_id), str(pair.right_id))) for pair in blocked.pairs
    }
    if blocked.truncated or blocked.skipped_blocks or actual_pairs != expected_pairs:
        frappe.throw(
            "Synthetic QC fixture did not produce exactly six isolated pairs: "
            + _json(
                {
                    "expected": len(expected_pairs),
                    "actual": len(actual_pairs),
                    "unexpected_pairs": sorted(
                        sorted(pair) for pair in actual_pairs - expected_pairs
                    ),
                    "missing_pairs": sorted(
                        sorted(pair) for pair in expected_pairs - actual_pairs
                    ),
                }
            )
        )

    edges: list[CanaryEdge] = []
    for pair in blocked.pairs:
        left = by_id[str(pair.left_id)]
        right = by_id[str(pair.right_id)]
        trusted = frozenset(
            attribute
            for attribute in policy.trusted_global_identifiers
            if policy.globally_comparable(left["source"], attribute)
            and policy.globally_comparable(right["source"], attribute)
        )
        result = tiered_result(
            build_evidence(left, right, policy),
            policy,
            conflict_mode="gated",
            trusted_identifiers=trusted,
        )
        if result.tier != MatchTier.HIGH or APPROVED_HIGH_REASON not in result.reasons:
            frappe.throw("A synthetic QC pair is not deterministic Tiered High")
        edges.append(
            CanaryEdge(
                str(pair.left_id),
                str(pair.right_id),
                left["source"],
                right["source"],
                pair.source_pair,
                tuple(pair.blocking_routes),
                tuple(result.reasons),
                True,
            )
        )
    return by_id, edges


def verify_synthetic_qc_automation_fixture(expect_pristine: int = 0) -> dict[str, Any]:
    """Fail closed if the isolated pair structure or expected state drifted."""
    manifest = inspect_synthetic_qc_automation_fixture()
    missing_masters = sorted(
        label for label, name in manifest["masters"].items() if not name
    )
    missing_raw = sorted(
        label for label, name in manifest["raw_records"].items() if not name
    )
    if missing_masters or missing_raw or not manifest["run"]:
        frappe.throw(
            "Synthetic QC automation fixture is incomplete: "
            + _json(
                {
                    "missing_masters": missing_masters,
                    "missing_raw": missing_raw,
                    "run_missing": not bool(manifest["run"]),
                }
            )
        )
    prerequisites = _canary_prerequisites(DEFAULT_PILOT_POLICY_VERSION)
    _records, edges = _build_edges(manifest["masters"], prerequisites["policy"])
    if len(edges) != PAIR_COUNT or len(manifest["recommendations"]) != PAIR_COUNT:
        frappe.throw("Synthetic QC automation fixture must contain exactly six pairs")
    if int(expect_pristine or 0):
        changed = [
            row["name"]
            for row in manifest["recommendations"]
            if row["status"] != "Proposed"
            or row["rollout_state"] != "Available"
            or row["qc_assigned_at"]
            or (
                row["qc_selected"]
                and row["qc_review_status"] != "Unreviewed"
            )
            or (
                not row["qc_selected"]
                and str(row["qc_review_status"] or "")
            )
        ]
        if changed:
            frappe.throw("Synthetic QC fixture is no longer pristine: " + ", ".join(changed))
        memberships = frappe.db.count(
            "CCD Identity Membership",
            {"ccd_master": ["in", list(manifest["masters"].values())]},
        )
        if memberships:
            frappe.throw("Pristine synthetic QC fixture already has Identity Memberships")
    return {
        **manifest,
        "verified": True,
        "expect_pristine": bool(int(expect_pristine or 0)),
        "isolated_pair_count": len(edges),
        "blocking_version": BLOCKING_VERSION,
    }


def _create_fixture() -> None:
    prerequisites = _canary_prerequisites(DEFAULT_PILOT_POLICY_VERSION)
    policy = prerequisites["policy"]
    masters: dict[str, str] = {}
    for label, person in PEOPLE.items():
        _ensure_raw_record(person)
        masters[label] = _ensure_master_record(person)
    by_id, edges = _build_edges(masters, policy)
    gate_records = {
        record_id: {
            "source": record["source"],
            "trusted_ids": _trusted_id_metadata(record, policy),
        }
        for record_id, record in by_id.items()
    }
    decisions = analyze_canary_edges(
        edges,
        gate_records,
        validated_source_pairs=prerequisites["validated_source_pairs"],
    )
    unexpected = {
        edge.pair_key: decisions[edge.pair_key].reasons
        for edge in edges
        if decisions[edge.pair_key].status != "Proposed"
    }
    if unexpected:
        frappe.throw("Synthetic QC pairs did not pass all gates: " + _json(unexpected))

    now = frappe.utils.now_datetime()
    run = frappe.get_doc(
        {
            "doctype": RUN_DOCTYPE,
            "matching_policy": DEFAULT_PILOT_POLICY_VERSION,
            "policy_version": policy.version,
            "policy_snapshot_json": _json(prerequisites["snapshot"]),
            "policy_snapshot_sha256": prerequisites["snapshot_sha256"],
            "high_validation_run": prerequisites["high_run"].name,
            "threshold_evaluation_run": prerequisites["threshold_run"].name,
            "splink_review_threshold": prerequisites["review_threshold"],
            "status": "Ready",
            "snapshot_at": now,
            "record_count": len(by_id),
            "candidate_count": len(edges),
            "candidate_truncated": 0,
            "skipped_blocks_json": "[]",
            "high_candidate_count": len(edges),
            "qc_automation_state": "Monitoring",
            "error_summary": _fixture_label(),
        }
    ).insert(ignore_permissions=True)

    created = []
    for edge in sorted(edges, key=lambda item: item.pair_key):
        decision = decisions[edge.pair_key]
        left_id, right_id = edge.pair_key
        left = by_id[left_id]
        right = by_id[right_id]
        recommendation = frappe.get_doc(
            {
                "doctype": RECOMMENDATION_DOCTYPE,
                "canary_run": run.name,
                "matching_policy": run.matching_policy,
                "policy_version": run.policy_version,
                "recommendation_key": _recommendation_key(
                    run.name, left_id, right_id
                ),
                "pair_fingerprint": _pair_fingerprint(
                    run.policy_version, left_id, right_id
                ),
                "left_record": left_id,
                "right_record": right_id,
                "left_source": left["source"],
                "right_source": right["source"],
                "source_pair": edge.source_pair,
                "left_modified_at": left["source_modified"],
                "right_modified_at": right["source_modified"],
                "left_identity_fingerprint": identity_fingerprint(left, policy),
                "right_identity_fingerprint": identity_fingerprint(right, policy),
                "model_tier": "High",
                "blocking_routes": ", ".join(edge.blocking_routes),
                "reason_codes_json": _json(edge.reason_codes),
                "cluster_fingerprint": decision.cluster_fingerprint,
                "cluster_size": decision.cluster_size,
                "status": "Proposed",
                "rollout_state": "Available",
                "safety_reasons_json": "[]",
                "qc_selected": 0,
            }
        ).insert(ignore_permissions=True)
        _append_event(
            recommendation,
            "Created",
            "",
            "Proposed",
            "development_only_synthetic_passed_all_canary_safety_gates",
            {"fixture_id": FIXTURE_ID},
        )
        created.append(str(recommendation.name))

    _refresh_run_counts(run.name)
    _initialize_review_workflow(run.name)
    # Production canaries start with up to 100 selected QC cases. This compact
    # fixture intentionally leaves three eligible recommendations unselected
    # so replenishment can be demonstrated without creating 100+ fake people.
    for recommendation_name in sorted(created)[INITIAL_QC_COUNT:]:
        frappe.db.set_value(
            RECOMMENDATION_DOCTYPE,
            recommendation_name,
            {
                "qc_selected": 0,
                "qc_review_status": None,
            },
            update_modified=False,
        )
    _initialize_review_workflow(run.name)  # refreshes counts; does not reselect
    summary = {
        "development_only_synthetic_fixture": True,
        "fixture_id": FIXTURE_ID,
        "record_ids": sorted(by_id),
        "recommendations": created,
        "component_count": PAIR_COUNT,
        "initial_qc_count": INITIAL_QC_COUNT,
        "blocking_version": BLOCKING_VERSION,
        "existing_ccd_records_modified": False,
        "identity_objects_created": False,
        "full_population_canary": False,
    }
    run.db_set(
        "summary_json", json.dumps(summary, ensure_ascii=False, sort_keys=True)
    )


def prepare_synthetic_qc_automation_fixture(confirm: str = "") -> dict[str, Any]:
    """Create the isolated fixture in one transaction (bench only)."""
    if str(confirm or "") != CONFIRMATION:
        frappe.throw(f"Confirmation must be exactly: {CONFIRMATION}")
    settings = frappe.get_single(SETTINGS_DOCTYPE)
    if materialization_enabled(automated=False):
        frappe.throw("Disable Identity Materialization before creating this fixture")
    if settings.automatic_tiered_enabled:
        frappe.throw("Stop Automatic Tiered before creating this fixture")
    if _existing_run():
        return verify_synthetic_qc_automation_fixture(expect_pristine=0)
    try:
        _create_fixture()
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        raise
    return verify_synthetic_qc_automation_fixture(expect_pristine=1)


def make_synthetic_qc_case_overdue(
    recommendation_name: str, confirm: str = ""
) -> dict[str, Any]:
    """Move one open assigned synthetic due date into the past (bench only)."""
    if str(confirm or "") != OVERDUE_CONFIRMATION:
        frappe.throw(f"Confirmation must be exactly: {OVERDUE_CONFIRMATION}")
    run_name = _existing_run()
    recommendation = frappe.get_doc(RECOMMENDATION_DOCTYPE, recommendation_name)
    if not run_name or str(recommendation.canary_run) != run_name:
        frappe.throw("The recommendation is not part of this synthetic fixture")
    if not recommendation.qc_assigned_at or recommendation.qc_review_status not in {
        "Unreviewed",
        "Partially Reviewed",
        "Positive Confirmation Required",
        "Needs Adjudication",
    }:
        frappe.throw("Select an open assigned synthetic QC case")
    due_at = frappe.utils.add_days(frappe.utils.now_datetime(), -1)
    recommendation.db_set("qc_due_at", due_at, update_modified=False)
    frappe.db.commit()
    return {"recommendation": recommendation.name, "qc_due_at": due_at}


def make_synthetic_qc_cadence_due(confirm: str = "") -> dict[str, Any]:
    """Move the fixture's next cadence time into the past (bench only)."""
    if str(confirm or "") != CADENCE_CONFIRMATION:
        frappe.throw(f"Confirmation must be exactly: {CADENCE_CONFIRMATION}")
    run_name = _existing_run()
    if not run_name:
        frappe.throw("Create the synthetic QC automation fixture first")
    next_at = frappe.utils.add_days(frappe.utils.now_datetime(), -1)
    frappe.db.set_value(
        RUN_DOCTYPE,
        run_name,
        "qc_next_assignment_at",
        next_at,
        update_modified=False,
    )
    frappe.db.commit()
    return {"run": run_name, "qc_next_assignment_at": next_at}
