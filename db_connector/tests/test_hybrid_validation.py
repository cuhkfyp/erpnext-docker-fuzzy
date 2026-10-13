import json
import random
import unittest
from pathlib import Path

from fuzzy_matching.hybrid_validation import (
    HYBRID_HOLDOUT_SIZE,
    HYBRID_VALIDATION_SIZE,
    dynamic_source_allocations,
    select_dynamic_hybrid_cohorts,
)


class HybridValidationSelectionTests(unittest.TestCase):
    def _rows(self):
        capacities = {
            "HMSSHP::SHP-UAT": 300,
            "DHCE::HMSSHP": 20,
            "HKSReCCMS::SHP-UAT": 5,
        }
        rows = []
        index = 0
        for source, count in capacities.items():
            for offset in range(count):
                index += 1
                rows.append(
                    {
                        "name": f"candidate-{index:04d}",
                        "source_pair": source,
                        "primary_route": (
                            "exact_name+unverified_identifier_exact"
                            if offset % 7 == 0
                            else "name_support+unverified_identifier+secondary_exact"
                        ),
                        "score_band": (
                            "0.999_to_ultra_high" if offset % 11 == 0 else "0.95_to_0.99"
                        ),
                    }
                )
        return rows, capacities

    def test_dynamic_source_allocation_uses_every_available_source(self):
        _rows, capacities = self._rows()
        allocations = dynamic_source_allocations(capacities)
        self.assertEqual(
            sum(allocations["validation"].values()), HYBRID_VALIDATION_SIZE
        )
        self.assertEqual(sum(allocations["holdout"].values()), HYBRID_HOLDOUT_SIZE)
        for source, capacity in capacities.items():
            self.assertGreaterEqual(allocations["validation"][source], 1)
            self.assertGreaterEqual(allocations["holdout"][source], 1)
            self.assertLessEqual(
                allocations["validation"][source]
                + allocations["holdout"][source],
                capacity,
            )

    def test_cohorts_are_exact_disjoint_and_order_independent(self):
        rows, _capacities = self._rows()
        seed = "a" * 64
        first = select_dynamic_hybrid_cohorts(rows, seed=seed)
        shuffled = list(rows)
        random.Random(99).shuffle(shuffled)
        second = select_dynamic_hybrid_cohorts(shuffled, seed=seed)
        self.assertEqual(first, second)
        self.assertEqual(len(first["validation"]), HYBRID_VALIDATION_SIZE)
        self.assertEqual(len(first["holdout"]), HYBRID_HOLDOUT_SIZE)
        self.assertFalse(set(first["validation"]) & set(first["holdout"]))

    def test_same_seed_reproduces_and_new_seed_changes_selection(self):
        rows, _capacities = self._rows()
        first = select_dynamic_hybrid_cohorts(rows, seed="1" * 64)
        repeated = select_dynamic_hybrid_cohorts(rows, seed="1" * 64)
        changed = select_dynamic_hybrid_cohorts(rows, seed="2" * 64)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first["validation"], changed["validation"])

    def test_population_must_fill_validation_and_holdout(self):
        with self.assertRaisesRegex(ValueError, "215 required"):
            dynamic_source_allocations({"HMSSHP::SHP-UAT": 214})


class HybridValidationIntegrationContractTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(__file__).resolve().parents[1]

    def test_desk_has_persistent_prepare_freeze_review_and_decision_actions(self):
        exploratory = (
            self.app
            / "db_connector/doctype/ccd_hybrid_exploratory_batch/ccd_hybrid_exploratory_batch.js"
        ).read_text(encoding="utf-8")
        evaluation = (
            self.app
            / "db_connector/doctype/ccd_match_evaluation_run/ccd_match_evaluation_run.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Prepare Development Validation Proposal", exploratory)
        self.assertIn("start_hybrid_validation_proposal", exploratory)
        self.assertIn("Freeze 165 Validation + 50 Holdout", evaluation)
        self.assertIn("start_freeze_hybrid_validation", evaluation)
        self.assertIn("Review Next Validation Pair", evaluation)
        self.assertIn("finalize_hybrid_prospective_validation", evaluation)
        self.assertIn("record_hybrid_development_decision", evaluation)
        self.assertIn("Development-only prospective evidence", evaluation)

    def test_schema_exposes_hybrid_provenance_without_reviewer_write_access(self):
        run_schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_match_evaluation_run/ccd_match_evaluation_run.json"
            ).read_text(encoding="utf-8")
        )
        pair_schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_match_evaluation_pair/ccd_match_evaluation_pair.json"
            ).read_text(encoding="utf-8")
        )
        purpose = next(
            field for field in run_schema["fields"] if field["fieldname"] == "run_purpose"
        )
        self.assertIn("Hybrid Prospective Validation", purpose["options"])
        run_fields = {field["fieldname"] for field in run_schema["fields"]}
        pair_fields = {field["fieldname"] for field in pair_schema["fields"]}
        self.assertTrue(
            {
                "hybrid_exploratory_batch",
                "hybrid_rule_version",
                "hybrid_eligible_manifest_digest",
                "hybrid_development_only",
            }.issubset(run_fields)
        )
        self.assertTrue(
            {
                "hybrid_primary_route",
                "hybrid_routes_json",
                "hybrid_score_band",
                "hybrid_birthday_status",
            }.issubset(pair_fields)
        )
        for permission in pair_schema["permissions"]:
            if permission["role"] in {
                "CCD Match Reviewer",
                "CCD Match Sensitive Reviewer",
            }:
                self.assertEqual(permission.get("write", 0), 0)

    def test_hybrid_reviews_are_masked_and_always_require_two_reviewers(self):
        source = (self.app / "api_fuzzy_evaluation.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("hybrid_prospective_validation"), 2)
        self.assertIn(
            '{"Splink Automatic Validation", "Hybrid Prospective Validation"}',
            source,
        )

    def test_worker_is_development_only_and_does_not_call_materialization(self):
        source = (self.app / "api_hybrid_validation.py").read_text(encoding="utf-8")
        self.assertIn('RUN_PURPOSE = "Hybrid Prospective Validation"', source)
        self.assertIn('"hybrid_development_only": 1', source)
        self.assertNotIn("apply_identity", source)
        self.assertNotIn("create_identity_group", source)
        self.assertNotIn("materialize_splink", source)
        self.assertIn("retry with that exact seed", source)
        self.assertIn(
            'method = "db_connector.api_hybrid_validation.prepare_hybrid_validation_proposal"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
