import importlib
import sys
import types
import unittest
from pathlib import Path

from fuzzy_matching.policy import MatchingPolicy, SourceProfile
from fuzzy_matching.types import CandidatePair, MatchTier


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

    def test_canonical_record_value_does_not_repeat_source_field_lookup(self):
        policy = MatchingPolicy(
            source_profiles={
                "A": SourceProfile("A", {"phone": "mobile"})
            }
        )
        canonical = {"record_id": "R1", "source": "A", "phone": "91234567"}
        self.assertEqual(policy.value(canonical, "phone"), "91234567")

    def test_positive_confirmation_requires_two_distinct_same_reviewers(self):
        rows = [
            types.SimpleNamespace(reviewer="reviewer-a", label="Same"),
            types.SimpleNamespace(reviewer="reviewer-a", label="Same"),
            types.SimpleNamespace(reviewer="reviewer-b", label="Unsure"),
        ]
        self.assertFalse(self.module._positive_confirmation_complete(rows))
        rows.append(types.SimpleNamespace(reviewer="reviewer-b", label="Same"))
        self.assertTrue(self.module._positive_confirmation_complete(rows))

    def test_positive_confirmation_preserves_randomized_assignment_reason(self):
        sampled = types.SimpleNamespace(
            needs_double_review=1,
            double_review_reason="sampled",
        )
        self.module._mark_positive_confirmation_required(sampled)
        self.assertEqual(sampled.double_review_reason, "sampled")

        newly_positive = types.SimpleNamespace(
            needs_double_review=0,
            double_review_reason="",
        )
        self.module._mark_positive_confirmation_required(newly_positive)
        self.assertEqual(newly_positive.needs_double_review, 1)
        self.assertEqual(newly_positive.double_review_reason, "positive_confirmation")

    def test_historical_pair_exclusion_is_orientation_independent(self):
        pairs = [
            CandidatePair("A1", "B1", "A::B", ("chi_full",)),
            CandidatePair("A2", "B2", "A::B", ("eng_name",)),
            CandidatePair("C1", "D1", "C::D", ("phone",)),
        ]
        counts, excluded = self.module._eligible_source_pair_counts(
            pairs,
            {self.module._ordered_pair_key("B1", "A1")},
        )
        self.assertEqual(excluded, 1)
        self.assertEqual(counts, {"A::B": 1, "C::D": 1})

    def test_hybrid_keeps_conflict_gate_and_requires_calibrated_high(self):
        conflict = self.module._hybrid_tier(MatchTier.CONFLICT.value, 0.999, 0.9, 0.6)
        high = self.module._hybrid_tier(MatchTier.REVIEW.value, 0.95, 0.9, 0.6)
        low = self.module._hybrid_tier(MatchTier.LOW.value, 0.95, 0.9, 0.6)
        preserved_review = self.module._hybrid_tier(
            MatchTier.REVIEW.value, 0.0, None, 0.6
        )
        uncalibrated_high = self.module._hybrid_tier(
            MatchTier.HIGH.value, 0.0, None, None
        )
        self.assertEqual(conflict, MatchTier.CONFLICT.value)
        self.assertEqual(high, MatchTier.HIGH.value)
        self.assertEqual(low, MatchTier.REVIEW.value)
        self.assertEqual(preserved_review, MatchTier.REVIEW.value)
        self.assertEqual(uncalibrated_high, MatchTier.REVIEW.value)

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
                    types.SimpleNamespace(sys_fieldname="hkid: HKID Number"),
                ]

        rows = self.module._registration_profile_rows(Registration())
        by_attribute = {row["canonical_attribute"]: row for row in rows}
        self.assertTrue(by_attribute["phone"]["enabled"])
        self.assertEqual(by_attribute["phone"]["fieldname"], "mobile")
        self.assertTrue(by_attribute["chi_firstname"]["enabled"])
        self.assertFalse(by_attribute["birthday"]["enabled"])
        self.assertTrue(by_attribute["hkid"]["enabled"])
        self.assertEqual(by_attribute["hkid"]["identifier_scope"], "Global")
        self.assertEqual(by_attribute["hkid"]["reliability_status"], "Approved")
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

    def test_positive_benchmark_selection_is_deterministic_balanced_and_deduplicated(self):
        rows = [
            {
                "left_id": f"A{index}",
                "right_id": f"B{index}",
                "source_pair": "A::B",
                "legacy_score": 0.95,
            }
            for index in range(90)
        ]
        rows.extend(
            {
                "left_id": f"C{index}",
                "right_id": f"D{index}",
                "source_pair": "C::D",
                "legacy_score": 0.95,
            }
            for index in range(10)
        )
        rows.append(dict(rows[0], legacy_score=0.99))
        first = self.module._select_positive_benchmark_rows(rows, 20, seed="benchmark")
        second = self.module._select_positive_benchmark_rows(
            list(reversed(rows)), 20, seed="benchmark"
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 20)
        self.assertEqual(
            {source: sum(row["source_pair"] == source for row in first) for source in ("A::B", "C::D")},
            {"A::B": 10, "C::D": 10},
        )
        self.assertEqual(len({(row["left_id"], row["right_id"]) for row in first}), 20)

    def test_high_tier_validation_is_uniform_deterministic_and_high_only(self):
        def result(index, source_pair, tier):
            pair = CandidatePair(
                f"L{index}",
                f"R{index}",
                source_pair,
                ("phone",),
            )
            model = types.SimpleNamespace(tier=tier)
            return types.SimpleNamespace(pair=pair, tiered_gated=model)

        rows = [
            result(index, "A::B" if index < 15 else "C::D", MatchTier.HIGH)
            for index in range(20)
        ]
        rows.append(result(100, "A::B", MatchTier.REVIEW))
        first, metadata = self.module._select_high_tier_validation_results(
            rows, 7, seed="high"
        )
        second, _ = self.module._select_high_tier_validation_results(
            reversed(rows), 7, seed="high"
        )
        self.assertEqual(
            [(item.pair.left_id, item.pair.right_id) for item in first],
            [(item.pair.left_id, item.pair.right_id) for item in second],
        )
        self.assertEqual(len(first), 7)
        self.assertTrue(all(item.tiered_gated.tier == MatchTier.HIGH for item in first))
        self.assertEqual(metadata["eligible_high_candidates"], 20)
        self.assertEqual(metadata["source_pair_counts"], {"A::B": 15, "C::D": 5})

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
        self.assertFalse(result["validation_ready"])
        self.assertIsNone(result["review_threshold"])
        self.assertEqual(result["warning"], "insufficient_positive_labels_per_split")

    def test_probability_repair_requires_explicit_finalized_reopening(self):
        mode = self.module._probability_repair_mode
        self.assertEqual(mode("Reviewing"), "normal")
        self.assertIsNone(mode("Scoring"))
        self.assertEqual(
            mode("Scoring", recover_stalled=True),
            "recover_stalled",
        )
        self.assertIsNone(mode("Completed"))
        self.assertEqual(
            mode("Completed", reopen_finalized=True),
            "reopen_finalized",
        )

    def test_calibration_requires_positive_labels_in_both_partitions(self):
        class Pair(dict):
            __getattr__ = dict.get

        calibration = []
        held_out = []
        index = 0
        while len(calibration) < 2 or len(held_out) < 2:
            pair = Pair(name=f"pair-{index}", final_label="Same", score=0.9)
            target = (
                calibration
                if self.module._stable_partition(pair.name) == "calibration"
                else held_out
            )
            if len(target) < 2:
                target.append(pair)
            index += 1
        rows = calibration + held_out
        ready = self.module._calibrate_scores(
            rows,
            "score",
            MatchingPolicy(
                minimum_high_samples=1,
                minimum_positive_labels_per_split=2,
            ),
        )
        self.assertTrue(ready["validation_ready"])
        self.assertIsNotNone(ready["review_threshold"])


if __name__ == "__main__":
    unittest.main()
