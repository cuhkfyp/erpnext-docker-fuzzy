import random
import json
import unittest
from pathlib import Path

from fuzzy_matching.tiered_one_to_many_validation import (
    HOLDOUT_SIZE,
    VALIDATION_SIZE,
    component_precision_gate,
    deterministic_qc_sample,
    holdout_and_combined_gate,
    select_component_cohorts,
    validate_seed,
)


class TieredOneToManyValidationTests(unittest.TestCase):
    def _rows(self):
        rows = []
        for index in range(330):
            rows.append(
                {
                    "name": f"component-{index:04d}",
                    "component_size": 3 + (index % 4),
                    "source_pattern": (
                        "HMSSHP:1|pjCHW:2" if index % 3 else "HMSGRH:1|HMSSHP:2"
                    ),
                }
            )
        return rows

    def test_seed_is_exactly_one_64_character_hex_value(self):
        self.assertEqual(validate_seed("A" * 64), "a" * 64)
        for value in ("a" * 63, "g" * 64, "", "a" * 65):
            with self.assertRaises(ValueError):
                validate_seed(value)

    def test_cohorts_are_exact_disjoint_and_order_independent(self):
        rows = self._rows()
        first = select_component_cohorts(rows, seed="1" * 64)
        shuffled = list(rows)
        random.Random(42).shuffle(shuffled)
        second = select_component_cohorts(shuffled, seed="1" * 64)
        self.assertEqual(first, second)
        self.assertEqual(len(first["validation"]), VALIDATION_SIZE)
        self.assertEqual(len(first["holdout"]), HOLDOUT_SIZE)
        self.assertFalse(set(first["validation"]) & set(first["holdout"]))
        self.assertTrue(first["validation_allocation"])
        self.assertTrue(first["holdout_allocation"])

    def test_validation_full_165_passes_with_two_non_same_but_not_three(self):
        passing = component_precision_gate(["All Same"] * 163 + ["Partial Match"] * 2)
        failing = component_precision_gate(["All Same"] * 162 + ["Partial Match"] * 3)
        self.assertTrue(passing["passed"])
        self.assertFalse(failing["passed"])

    def test_pair_level_staleness_uses_150_and_45_attrition_floors(self):
        validation_pass = component_precision_gate(
            ["All Same"] * 150, stale_count=15
        )
        validation_fail = component_precision_gate(
            ["All Same"] * 149, stale_count=16
        )
        combined_pass = holdout_and_combined_gate(
            ["All Same"] * 150,
            ["All Same"] * 45,
            validation_stale_count=15,
            holdout_stale_count=5,
        )
        combined_fail = holdout_and_combined_gate(
            ["All Same"] * 150,
            ["All Same"] * 44,
            validation_stale_count=15,
            holdout_stale_count=6,
        )
        self.assertTrue(validation_pass["passed"])
        self.assertFalse(validation_fail["passed"])
        self.assertTrue(combined_pass["combined"]["passed"])
        self.assertFalse(combined_fail["combined"]["passed"])

    def test_holdout_is_full_qc_but_combined_gate_authorizes(self):
        result = holdout_and_combined_gate(["All Same"] * 165, ["All Same"] * 50)
        self.assertTrue(result["combined"]["passed"])
        self.assertEqual(result["holdout"]["valid"], 50)

    def test_ongoing_qc_is_a_bounded_deterministic_sample(self):
        names = [f"component-{index}" for index in range(100)]
        first = deterministic_qc_sample(
            names, seed="f" * 64, batch_key="batch-1", count=7
        )
        second = deterministic_qc_sample(
            reversed(names), seed="f" * 64, batch_key="batch-1", count=7
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 7)


class TieredOneToManyDeskContractTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(__file__).resolve().parents[1]

    def test_desk_exposes_prepare_validation_holdout_and_bulk_actions(self):
        settings = (
            self.app
            / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.js"
        ).read_text(encoding="utf-8")
        validation = (
            self.app
            / "db_connector/doctype/ccd_tiered_one_to_many_validation/ccd_tiered_one_to_many_validation.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Create One-to-Many Validation Proposal", settings)
        self.assertIn("Preview One-to-Many Bulk Cycle", settings)
        self.assertIn("Run One One-to-Many Cycle Now", settings)
        self.assertIn("Freeze 165 Validation + 50 Holdout", validation)
        self.assertIn("Review Next Validation Component", validation)
        self.assertIn("Create Blinded Holdout Rollout", validation)
        self.assertIn("Review Next Holdout Component", validation)
        self.assertIn("Apply Final Validation Decisions", validation)
        self.assertIn("Review Next Ongoing QC Component", validation)

    def test_schema_persists_component_cohorts_and_sampled_qc(self):
        component = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_match_component_review/ccd_match_component_review.json"
            ).read_text(encoding="utf-8")
        )
        settings = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.json"
            ).read_text(encoding="utf-8")
        )
        component_fields = {field["fieldname"] for field in component["fields"]}
        settings_fields = {field["fieldname"] for field in settings["fields"]}
        self.assertTrue(
            {
                "tiered_one_to_many_validation",
                "tiered_governance_cohort",
                "tiered_automation_status",
                "tiered_qc_assigned_at",
            }.issubset(component_fields)
        )
        self.assertTrue(
            {
                "automatic_tiered_one_to_many_enabled",
                "tiered_one_to_many_components_per_run",
                "tiered_one_to_many_qc_percent",
            }.issubset(settings_fields)
        )

    def test_holdout_is_full_qc_but_bulk_uses_only_deterministic_sample(self):
        service = (self.app / "api_tiered_one_to_many.py").read_text(
            encoding="utf-8"
        )
        workbench = (
            self.app
            / "db_connector/report/ccd_unified_qc_workbench/ccd_unified_qc_workbench.py"
        ).read_text(encoding="utf-8")
        self.assertIn('batch.batch_type == "Blinded Holdout Rollout" and applied', service)
        self.assertIn("deterministic_qc_sample", service)
        self.assertIn("tiered_one_to_many_qc_percent", service)
        self.assertIn("shared_weekly_qc_capacity", service)
        self.assertIn('CHANNEL_ONE_TO_MANY = "Tiered One-to-Many"', workbench)

    def test_frappe_controller_names_preserve_lowercase_hyphenated_to(self):
        validation_controller = (
            self.app
            / "db_connector/doctype/ccd_tiered_one_to_many_validation/ccd_tiered_one_to_many_validation.py"
        ).read_text(encoding="utf-8")
        batch_controller = (
            self.app
            / "db_connector/doctype/ccd_tiered_one_to_many_batch/ccd_tiered_one_to_many_batch.py"
        ).read_text(encoding="utf-8")
        self.assertIn("class CCDTieredOnetoManyValidation", validation_controller)
        self.assertIn("class CCDTieredOnetoManyBatch", batch_controller)


if __name__ == "__main__":
    unittest.main()
