import importlib
import sys
import types
import unittest
from pathlib import Path

from fuzzy_matching.policy import MatchingPolicy, SourceProfile
from fuzzy_matching.types import MatchTier


class EvaluationHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_root = str(Path(__file__).resolve().parents[2])
        if app_root not in sys.path:
            sys.path.insert(0, app_root)
        fake_frappe = types.SimpleNamespace(whitelist=lambda: (lambda function: function))
        sys.modules.setdefault("frappe", fake_frappe)
        cls.module = importlib.import_module("db_connector.api_fuzzy_evaluation")

    def test_policy_snapshot_round_trip_preserves_identifier_scope(self):
        policy = MatchingPolicy(
            version="2.0.0",
            source_profiles={
                "A": SourceProfile("A", {"hkid": "hkid_num"}, {"hkid": "global"})
            },
            trusted_global_identifiers=frozenset({"hkid"}),
        )
        restored = MatchingPolicy.from_dict(self.module._policy_snapshot(policy))
        self.assertTrue(restored.globally_comparable("A", "hkid"))
        self.assertEqual(restored.profile("A").field_for("hkid"), "hkid_num")

    def test_hybrid_keeps_conflict_gate_and_requires_calibrated_high(self):
        conflict = self.module._hybrid_tier(MatchTier.CONFLICT.value, 0.999, 0.9, 0.6)
        high = self.module._hybrid_tier(MatchTier.REVIEW.value, 0.95, 0.9, 0.6)
        low = self.module._hybrid_tier(MatchTier.LOW.value, 0.95, 0.9, 0.6)
        self.assertEqual(conflict, MatchTier.CONFLICT.value)
        self.assertEqual(high, MatchTier.HIGH.value)
        self.assertEqual(low, MatchTier.REVIEW.value)

    def test_registration_mapping_imports_identity_targets_only(self):
        class Registration:
            name = "SOURCE_A"

            def get(self, fieldname):
                self.assert_fieldname = fieldname
                return [
                    types.SimpleNamespace(sys_fieldname="mobile: Mobile Phone"),
                    types.SimpleNamespace(sys_fieldname="res_addr1: Address 1"),
                    types.SimpleNamespace(sys_fieldname="chi_firstname: Chinese Firstname"),
                    types.SimpleNamespace(sys_fieldname="birthday_key: Birthday Key"),
                ]

        rows = self.module._registration_profile_rows(Registration())
        by_attribute = {row["canonical_attribute"]: row for row in rows}
        self.assertTrue(by_attribute["phone"]["enabled"])
        self.assertEqual(by_attribute["phone"]["fieldname"], "mobile")
        self.assertTrue(by_attribute["chi_firstname"]["enabled"])
        self.assertFalse(by_attribute["birthday"]["enabled"])
        self.assertNotIn("res_addr1", {row["fieldname"] for row in rows})

    def test_probability_training_sample_is_bounded_and_keeps_review_records(self):
        records = [{"record_id": f"R{index}", "source": "A"} for index in range(100)]
        first = self.module._bounded_probability_records(
            records,
            {"R95", "R96"},
            limit=10,
        )
        second = self.module._bounded_probability_records(
            list(reversed(records)),
            {"R95", "R96"},
            limit=10,
        )
        first_ids = {row["record_id"] for row in first}
        self.assertEqual(len(first), 10)
        self.assertTrue({"R95", "R96"}.issubset(first_ids))
        self.assertEqual(first_ids, {row["record_id"] for row in second})

    def test_probability_calibration_distinguishes_missing_from_real_zero(self):
        class Pair(dict):
            __getattr__ = dict.get

        pairs = [
            Pair(
                name="missing",
                final_label="Different",
                probabilistic_score=0,
                probabilistic_available=0,
            ),
            Pair(
                name="real-zero",
                final_label="Different",
                probabilistic_score=0,
                probabilistic_available=1,
            ),
        ]
        result = self.module._calibrate_scores(
            pairs,
            "probabilistic_score",
            MatchingPolicy(minimum_high_samples=30),
        )
        self.assertEqual(result["available_pairs"], 1)


if __name__ == "__main__":
    unittest.main()
