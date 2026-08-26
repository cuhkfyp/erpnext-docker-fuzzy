"""Development-only synthetic fixtures for identity-overlap testing.

The fixture deliberately uses the installed ``pilot-1.6`` policy and its
approved validation evidence, but scopes each synthetic canary to one frozen
pair.  This avoids duplicating recommendations for the full CCD population.

It creates source-staging rows, CCD Master rows, canary runs,
recommendations, and exception-component reviews.  It never creates an
Identity Decision, Identity Group, Identity Membership, or Different
exclusion.  Materialization must be disabled before the fixture is created.
"""

from __future__ import annotations

import json
from typing import Any

import frappe

from db_connector.api_fuzzy_canary import (
    APPROVED_HIGH_REASON,
    COMPONENT_REVIEW_DOCTYPE,
    RECOMMENDATION_DOCTYPE,
    RUN_DOCTYPE,
    _append_event,
    _canary_prerequisites,
    _component_review_key,
    _json,
    _pair_fingerprint,
    _recommendation_key,
    _refresh_review_workflow_counts,
    _refresh_run_counts,
    _trusted_id_metadata,
)
from db_connector.api_fuzzy_evaluation import (
    DEFAULT_PILOT_POLICY_VERSION,
    _canonical_record,
)
from db_connector.api_identity_resolution import materialization_enabled
from db_connector.fuzzy_matching.blocking import BLOCKING_VERSION, generate_candidate_pairs
from db_connector.fuzzy_matching.canary import CanaryEdge, analyze_canary_edges
from db_connector.fuzzy_matching.identity import identity_fingerprint
from db_connector.fuzzy_matching.models import build_evidence, tiered_result
from db_connector.fuzzy_matching.types import MatchTier


FIXTURE_ID = "synthetic-overlap-v1-20260826"
CONFIRMATION = "CREATE DEVELOPMENT SYNTHETIC OVERLAP FIXTURE"

SOURCES = {
    "DHCE": {
        "source": "HQ-vDB01_DHCE_Prod",
        "raw_doctype": "CCD-REG-HQ-vDB01_DHCE_Prod-1",
    },
    "HMSSHP": {
        "source": "HQ-vDB01_HMSSHP_Prod",
        "raw_doctype": "HQ-vDB01-HMSSHP_Prod",
    },
    "HKSRE": {
        "source": "HQ-vDB01_HKSReCCMS_PROD",
        "raw_doctype": "CCD-REG-HQ-vDB01_HKSReCCMS_PROD",
    },
    "PHI_SIT": {
        "source": "PHI-vDBUAT_HMSPhi_SIT",
        "raw_doctype": "CCD-REG-PHI-vDBUAT_HMSPhi_SIT",
    },
    "PHI_UAT": {
        "source": "PHI-vDBUAT_HMSPhi_UAT",
        "raw_doctype": "CCD-REG-PHI-vDBUAT_HMSPhi_UAT",
    },
}


def _person(
    key: str,
    source: str,
    surname: str,
    firstname: str,
    *,
    phone: str,
    email: str,
) -> dict[str, str]:
    source_config = SOURCES[source]
    return {
        "fixture_record": key,
        "ccd_source_key": f"SYNTH-OVL-20260826-{key}",
        "source": str(source_config["source"]),
        "raw_doctype": str(source_config["raw_doctype"]),
        "chi_surname": surname,
        "chi_firstname": firstname,
        "eng_surname": "SYNTHETIC",
        "eng_firstname": key.replace("_", " "),
        "phone_num": phone,
        "email": email,
    }


# A and C in each scenario have the same name but no shared independent exact
# field.  A-B is joined by phone; B-C is joined by email.  Therefore the
# fixture cannot accidentally create a third deterministic-High A-C edge.
PEOPLE = {
    # Tiered -> Exception: HMSSHP-DHCE is validated; DHCE-PHI SIT is not.
    "TE_A": _person(
        "TE_A", "HMSSHP", "測試", "交疊甲", phone="99010001", email="te-a@example.invalid"
    ),
    "TE_B": _person(
        "TE_B", "DHCE", "測試", "交疊甲", phone="99010001", email="te-bridge@example.invalid"
    ),
    "TE_C": _person(
        "TE_C", "PHI_SIT", "測試", "交疊甲", phone="99010003", email="te-bridge@example.invalid"
    ),
    # Tiered -> Tiered: DHCE-HMSSHP and HMSSHP-HKSReCCMS are both validated.
    "TT_A": _person(
        "TT_A", "DHCE", "測試", "交疊乙", phone="99020001", email="tt-a@example.invalid"
    ),
    "TT_B": _person(
        "TT_B", "HMSSHP", "測試", "交疊乙", phone="99020001", email="tt-bridge@example.invalid"
    ),
    "TT_C": _person(
        "TT_C", "HKSRE", "測試", "交疊乙", phone="99020003", email="tt-bridge@example.invalid"
    ),
    # Exception -> Exception: both DHCE-PHI pairs are outside validated coverage.
    "EE_A": _person(
        "EE_A", "PHI_SIT", "測試", "交疊丙", phone="99030001", email="ee-a@example.invalid"
    ),
    "EE_B": _person(
        "EE_B", "DHCE", "測試", "交疊丙", phone="99030001", email="ee-bridge@example.invalid"
    ),
    "EE_C": _person(
        "EE_C", "PHI_UAT", "測試", "交疊丙", phone="99030003", email="ee-bridge@example.invalid"
    ),
}

SCOPES = (
    {
        "key": "tiered_exception_baseline",
        "scenario": "Tiered Evidence <-> Exception",
        "stage": "1 - apply baseline first",
        "left": "TE_A",
        "right": "TE_B",
        "expected_status": "Proposed",
    },
    {
        "key": "tiered_exception_later",
        "scenario": "Tiered Evidence <-> Exception",
        "stage": "2 - review after baseline is applied",
        "left": "TE_B",
        "right": "TE_C",
        "expected_status": "Exception",
    },
    {
        "key": "tiered_tiered_baseline",
        "scenario": "Tiered Evidence <-> Tiered Evidence",
        "stage": "1 - apply baseline first",
        "left": "TT_A",
        "right": "TT_B",
        "expected_status": "Proposed",
    },
    {
        "key": "tiered_tiered_later",
        "scenario": "Tiered Evidence <-> Tiered Evidence",
        "stage": "2 - prepare overlap batch after baseline is applied",
        "left": "TT_B",
        "right": "TT_C",
        "expected_status": "Proposed",
    },
    {
        "key": "exception_exception_baseline",
        "scenario": "Exception <-> Exception",
        "stage": "1 - review and apply baseline first",
        "left": "EE_A",
        "right": "EE_B",
        "expected_status": "Exception",
    },
    {
        "key": "exception_exception_later",
        "scenario": "Exception <-> Exception",
        "stage": "2 - review after baseline is applied",
        "left": "EE_B",
        "right": "EE_C",
        "expected_status": "Exception",
    },
)


def _fixture_label(scope_key: str) -> str:
    return f"DEVELOPMENT_ONLY_SYNTHETIC:{FIXTURE_ID}:{scope_key}"


def _raw_values(person: dict[str, str]) -> dict[str, Any]:
    """Project canonical fixture fields into the installed staging DocType."""
    doc = frappe.new_doc(person["raw_doctype"])
    available = {field.fieldname for field in doc.meta.fields}
    candidates = {
        "ccd_source_key": ("ccd_source_key",),
        "chi_surname": ("lastname_cn", "chi_surname"),
        "chi_firstname": ("firstname_cn", "chi_firstname"),
        "eng_surname": ("lastname_en", "eng_surname"),
        "eng_firstname": ("firstname_en", "eng_firstname"),
        "phone_num": ("mobileno", "mobile", "phone_num"),
        "email": ("contactemail", "email"),
    }
    output: dict[str, Any] = {"doctype": person["raw_doctype"]}
    for source_field, possible_fields in candidates.items():
        target = next((field for field in possible_fields if field in available), "")
        if target:
            output[target] = person[source_field]
    if output.get("ccd_source_key") != person["ccd_source_key"]:
        frappe.throw(f"{person['raw_doctype']} has no CCD Source Key field")
    return output


def _assert_values(doctype: str, name: str, expected: dict[str, Any]) -> None:
    doc = frappe.get_doc(doctype, name)
    differences = [
        field
        for field, value in expected.items()
        if field != "doctype" and str(doc.get(field) or "") != str(value or "")
    ]
    if differences:
        frappe.throw(
            f"Existing synthetic record {doctype} {name} differs in: "
            + ", ".join(sorted(differences))
        )


def _ensure_raw_record(person: dict[str, str]) -> str:
    values = _raw_values(person)
    existing = frappe.db.get_value(
        person["raw_doctype"], {"ccd_source_key": person["ccd_source_key"]}, "name"
    )
    if existing:
        _assert_values(person["raw_doctype"], str(existing), values)
        return str(existing)
    return str(frappe.get_doc(values).insert(ignore_permissions=True).name)


def _master_values(person: dict[str, str]) -> dict[str, Any]:
    return {
        "doctype": "CCD Master",
        "ccd_reg_source": person["source"],
        "ccd_source_key": person["ccd_source_key"],
        "ccd_primary_key": person["ccd_source_key"],
        "chi_surname": person["chi_surname"],
        "chi_firstname": person["chi_firstname"],
        "eng_surname": person["eng_surname"],
        "eng_firstname": person["eng_firstname"],
        "phone_num": person["phone_num"],
        "email": person["email"],
    }


def _ensure_master_record(person: dict[str, str]) -> str:
    values = _master_values(person)
    existing = frappe.db.get_value(
        "CCD Master",
        {
            "ccd_reg_source": person["source"],
            "ccd_source_key": person["ccd_source_key"],
        },
        "name",
    )
    if existing:
        _assert_values("CCD Master", str(existing), values)
        return str(existing)
    return str(frappe.get_doc(values).insert(ignore_permissions=True).name)


def _existing_scope(scope_key: str) -> dict[str, str] | None:
    run_name = frappe.db.get_value(
        RUN_DOCTYPE, {"error_summary": _fixture_label(scope_key)}, "name"
    )
    if not run_name:
        return None
    recommendations = frappe.get_all(
        RECOMMENDATION_DOCTYPE,
        filters={"canary_run": run_name},
        fields=["name", "component_review", "status", "left_record", "right_record"],
        limit_page_length=2,
    )
    if len(recommendations) != 1:
        frappe.throw(f"Synthetic canary {run_name} is incomplete or ambiguous")
    recommendation = recommendations[0]
    return {
        "run": str(run_name),
        "recommendation": str(recommendation.name),
        "component_review": str(recommendation.component_review or ""),
        "status": str(recommendation.status),
        "left_record": str(recommendation.left_record),
        "right_record": str(recommendation.right_record),
    }


def _create_scope(
    scope: dict[str, str],
    masters: dict[str, str],
    prerequisites: dict[str, Any],
) -> dict[str, str]:
    existing = _existing_scope(scope["key"])
    if existing:
        if existing["status"] != scope["expected_status"]:
            # A completed test naturally changes Proposed to Approved.  The
            # existing fixture remains authoritative and must not be duplicated.
            existing["current_status"] = existing["status"]
        return {**scope, **existing}

    policy = prerequisites["policy"]
    master_docs = {
        label: frappe.get_doc("CCD Master", masters[label])
        for label in (scope["left"], scope["right"])
    }
    records = {
        label: _canonical_record(dict(master.as_dict()), policy)
        for label, master in master_docs.items()
    }
    blocked = generate_candidate_pairs(list(records.values()), policy)
    intended_ids = {masters[scope["left"]], masters[scope["right"]]}
    pairs = [
        pair
        for pair in blocked.pairs
        if {str(pair.left_id), str(pair.right_id)} == intended_ids
    ]
    if blocked.truncated or blocked.skipped_blocks or len(pairs) != 1:
        frappe.throw(
            f"Synthetic scope {scope['key']} did not produce exactly one complete candidate"
        )
    pair = pairs[0]
    by_id = {
        str(record["record_id"]): record
        for record in records.values()
    }
    left = by_id[str(pair.left_id)]
    right = by_id[str(pair.right_id)]
    evidence = build_evidence(left, right, policy)
    trusted = frozenset(
        attribute
        for attribute in policy.trusted_global_identifiers
        if policy.globally_comparable(left["source"], attribute)
        and policy.globally_comparable(right["source"], attribute)
    )
    result = tiered_result(
        evidence,
        policy,
        conflict_mode="gated",
        trusted_identifiers=trusted,
    )
    if result.tier != MatchTier.HIGH or APPROVED_HIGH_REASON not in result.reasons:
        frappe.throw(
            f"Synthetic scope {scope['key']} is not an approved deterministic High pair"
        )

    edge = CanaryEdge(
        str(pair.left_id),
        str(pair.right_id),
        left["source"],
        right["source"],
        pair.source_pair,
        tuple(pair.blocking_routes),
        tuple(result.reasons),
        True,
    )
    gate_records = {
        record_id: {
            "source": row["source"],
            "trusted_ids": _trusted_id_metadata(row, policy),
        }
        for record_id, row in by_id.items()
    }
    decision = analyze_canary_edges(
        [edge],
        gate_records,
        validated_source_pairs=prerequisites["validated_source_pairs"],
    )[edge.pair_key]
    if decision.status != scope["expected_status"]:
        frappe.throw(
            f"Synthetic scope {scope['key']} expected {scope['expected_status']} "
            f"but the current policy produced {decision.status}: {decision.reasons}"
        )

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
            "record_count": 2,
            "candidate_count": 1,
            "candidate_truncated": 0,
            "skipped_blocks_json": "[]",
            "high_candidate_count": 1,
            "qc_automation_state": "Monitoring",
            "error_summary": _fixture_label(scope["key"]),
        }
    ).insert(ignore_permissions=True)

    left_id, right_id = edge.pair_key
    frozen_left = by_id[left_id]
    frozen_right = by_id[right_id]
    recommendation = frappe.get_doc(
        {
            "doctype": RECOMMENDATION_DOCTYPE,
            "canary_run": run.name,
            "matching_policy": run.matching_policy,
            "policy_version": run.policy_version,
            "recommendation_key": _recommendation_key(run.name, left_id, right_id),
            "pair_fingerprint": _pair_fingerprint(run.policy_version, left_id, right_id),
            "left_record": left_id,
            "right_record": right_id,
            "left_source": frozen_left["source"],
            "right_source": frozen_right["source"],
            "source_pair": edge.source_pair,
            "left_modified_at": frozen_left["source_modified"],
            "right_modified_at": frozen_right["source_modified"],
            "left_identity_fingerprint": identity_fingerprint(frozen_left, policy),
            "right_identity_fingerprint": identity_fingerprint(frozen_right, policy),
            "model_tier": "High",
            "blocking_routes": ", ".join(edge.blocking_routes),
            "reason_codes_json": _json(edge.reason_codes),
            "cluster_fingerprint": decision.cluster_fingerprint,
            "cluster_size": decision.cluster_size,
            "status": decision.status,
            "rollout_state": "Available",
            "safety_reasons_json": _json(decision.reasons),
            "qc_selected": 0,
        }
    ).insert(ignore_permissions=True)

    review_name = ""
    if decision.status == "Exception":
        review = frappe.get_doc(
            {
                "doctype": COMPONENT_REVIEW_DOCTYPE,
                "canary_run": run.name,
                "review_key": _component_review_key(
                    run.name, decision.cluster_fingerprint
                ),
                "cluster_fingerprint": decision.cluster_fingerprint,
                "cluster_size": decision.cluster_size,
                "recommendation_count": 1,
                "review_status": "Unreviewed",
                "materialization_status": "Not Final",
            }
        ).insert(ignore_permissions=True)
        review_name = str(review.name)
        recommendation.db_set("component_review", review.name, update_modified=False)

    _append_event(
        recommendation,
        "Created" if decision.status == "Proposed" else "Safety Exception",
        "",
        decision.status,
        (
            "development_only_synthetic_passed_all_canary_safety_gates"
            if decision.status == "Proposed"
            else "development_only_synthetic:" + ",".join(decision.reasons)
        ),
        {"fixture_id": FIXTURE_ID, "scope": scope["key"]},
    )
    _refresh_run_counts(run.name)
    _refresh_review_workflow_counts(run.name)
    summary = {
        "development_only_synthetic_fixture": True,
        "fixture_id": FIXTURE_ID,
        "scope": scope["key"],
        "scenario": scope["scenario"],
        "stage": scope["stage"],
        "record_ids": sorted((left_id, right_id)),
        "blocking_version": BLOCKING_VERSION,
        "blocking_routes": sorted(edge.blocking_routes),
        "reason_codes": list(edge.reason_codes),
        "safety_reasons": list(decision.reasons),
        "expected_status": scope["expected_status"],
        "existing_ccd_records_modified": False,
        "synthetic_ccd_records_created": 2,
        "identity_objects_created": False,
        "full_population_canary": False,
    }
    run.db_set("summary_json", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return {
        **scope,
        "run": str(run.name),
        "recommendation": str(recommendation.name),
        "component_review": review_name,
        "status": decision.status,
        "left_record": left_id,
        "right_record": right_id,
    }


def inspect_synthetic_overlap_fixture() -> dict[str, Any]:
    """Return the current fixture manifest without changing data."""
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
    scopes = []
    for scope in SCOPES:
        current = _existing_scope(scope["key"])
        scopes.append({**scope, **(current or {})})
    return {
        "fixture_id": FIXTURE_ID,
        "development_only": True,
        "materialization_enabled": materialization_enabled(automated=False),
        "masters": masters,
        "raw_records": raw_records,
        "scopes": scopes,
    }


def _model_tier_for_labels(
    left_label: str,
    right_label: str,
    masters: dict[str, str],
    policy: Any,
) -> str:
    records = []
    for label in (left_label, right_label):
        if not masters.get(label):
            return "Missing"
        records.append(
            _canonical_record(
                dict(frappe.get_doc("CCD Master", masters[label]).as_dict()), policy
            )
        )
    blocked = generate_candidate_pairs(records, policy)
    if len(blocked.pairs) != 1:
        return "No unique candidate"
    pair = blocked.pairs[0]
    by_id = {str(record["record_id"]): record for record in records}
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
    return str(result.tier.value)


def verify_synthetic_overlap_fixture(expect_pristine: int = 0) -> dict[str, Any]:
    """Fail closed if fixture structure or its isolated evidence has drifted."""
    manifest = inspect_synthetic_overlap_fixture()
    missing_masters = sorted(
        label for label, name in manifest["masters"].items() if not name
    )
    missing_raw = sorted(
        label for label, name in manifest["raw_records"].items() if not name
    )
    missing_scopes = sorted(
        scope["key"] for scope in manifest["scopes"] if not scope.get("run")
    )
    if missing_masters or missing_raw or missing_scopes:
        frappe.throw(
            "Synthetic fixture is incomplete: "
            + _json(
                {
                    "missing_masters": missing_masters,
                    "missing_raw": missing_raw,
                    "missing_scopes": missing_scopes,
                }
            )
        )

    prerequisites = _canary_prerequisites(DEFAULT_PILOT_POLICY_VERSION)
    policy = prerequisites["policy"]
    bridge_checks = {
        "tiered_exception_A_C": _model_tier_for_labels(
            "TE_A", "TE_C", manifest["masters"], policy
        ),
        "tiered_tiered_A_C": _model_tier_for_labels(
            "TT_A", "TT_C", manifest["masters"], policy
        ),
        "exception_exception_A_C": _model_tier_for_labels(
            "EE_A", "EE_C", manifest["masters"], policy
        ),
    }
    high_bridge_edges = sorted(
        key for key, tier in bridge_checks.items() if tier == MatchTier.HIGH.value
    )
    if high_bridge_edges:
        frappe.throw("Synthetic A-C isolation failed: " + ", ".join(high_bridge_edges))

    scope_checks = []
    for scope in manifest["scopes"]:
        recommendation = frappe.get_doc(
            RECOMMENDATION_DOCTYPE, scope["recommendation"]
        )
        safety_reasons = json.loads(recommendation.safety_reasons_json or "[]")
        if scope["expected_status"] == "Exception" and safety_reasons != [
            "unvalidated_source_pair"
        ]:
            frappe.throw(
                f"{scope['key']} has unexpected exception reasons: {safety_reasons}"
            )
        if scope["expected_status"] == "Proposed" and safety_reasons:
            frappe.throw(f"{scope['key']} unexpectedly has safety reasons")
        if int(expect_pristine or 0) and recommendation.status != scope["expected_status"]:
            frappe.throw(
                f"{scope['key']} is no longer pristine: {recommendation.status}"
            )
        scope_checks.append(
            {
                "key": scope["key"],
                "current_status": str(recommendation.status),
                "expected_initial_status": scope["expected_status"],
                "safety_reasons": safety_reasons,
                "component_review": str(recommendation.component_review or ""),
            }
        )

    master_names = list(manifest["masters"].values())
    membership_count = frappe.db.count(
        "CCD Identity Membership", {"ccd_master": ["in", master_names]}
    )
    if int(expect_pristine or 0) and membership_count:
        frappe.throw("Pristine synthetic fixture already has Identity Memberships")
    return {
        **manifest,
        "verified": True,
        "expect_pristine": bool(int(expect_pristine or 0)),
        "bridge_pair_tiers": bridge_checks,
        "identity_membership_count": int(membership_count),
        "scope_checks": scope_checks,
    }


def prepare_synthetic_overlap_fixture(confirm: str = "") -> dict[str, Any]:
    """Create all isolated fixtures in one transaction.

    This bench-only function is intentionally not whitelisted for browser use.
    """
    if str(confirm or "") != CONFIRMATION:
        frappe.throw(f"Confirmation must be exactly: {CONFIRMATION}")
    if materialization_enabled(automated=False):
        frappe.throw("Disable Identity Materialization before creating synthetic fixtures")

    try:
        prerequisites = _canary_prerequisites(DEFAULT_PILOT_POLICY_VERSION)
        masters: dict[str, str] = {}
        for label, person in PEOPLE.items():
            _ensure_raw_record(person)
            masters[label] = _ensure_master_record(person)
        for scope in SCOPES:
            _create_scope(scope, masters, prerequisites)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        raise
    return inspect_synthetic_overlap_fixture()
