import unittest
from collections import Counter

from fuzzy_matching.splink_automation import (
    FROZEN_AUTOMATIC_CUTOFF,
    allocate_shared_capacity,
    authorization_staleness,
    automatic_components,
    canonical_source_pair,
    component_metadata,
    select_stratified_cohorts,
    validation_gate,
)


class SplinkAutomationHelperTests(unittest.TestCase):
    def _cohort_rows(self):
        counts = {
            "DHCE::HMSSHP": 177,
            "HKSReCCMS::SHP-UAT": 26,
            "HMSSHP::SHP-UAT": 11,
            "PHI-UAT::SHP-UAT": 1,
        }
        rows = []
        cursor = 0
        for source_pair, count in counts.items():
            for _ in range(count):
                cursor += 1
                rows.append(
                    {
                        "name": f"C{cursor:03d}",
                        "pair_fingerprint": f"F{cursor:03d}",
                        "source_pair": source_pair,
                    }
                )
        return rows

    def test_frozen_cohort_is_deterministic_disjoint_and_exact(self):
        rows = self._cohort_rows()
        first = select_stratified_cohorts(rows, seed="queue:gkd75jb71h:2026-10")
        second = select_stratified_cohorts(reversed(rows), seed="queue:gkd75jb71h:2026-10")
        self.assertEqual(first, second)
        self.assertEqual(len(first["validation"]), 165)
        self.assertEqual(len(first["holdout"]), 50)
        self.assertFalse(set(first["validation"]) & set(first["holdout"]))
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(
            Counter(by_name[name]["source_pair"] for name in first["validation"]),
            Counter(
                {
                    "DHCE::HMSSHP": 136,
                    "HKSReCCMS::SHP-UAT": 20,
                    "HMSSHP::SHP-UAT": 8,
                    "PHI-UAT::SHP-UAT": 1,
                }
            ),
        )
        self.assertEqual(
            Counter(by_name[name]["source_pair"] for name in first["holdout"]),
            Counter(
                {
                    "DHCE::HMSSHP": 41,
                    "HKSReCCMS::SHP-UAT": 6,
                    "HMSSHP::SHP-UAT": 3,
                }
            ),
        )

    def test_known_labels_are_not_available_to_formal_cohort(self):
        rows = self._cohort_rows()
        rows[0]["final_label"] = "Same"
        with self.assertRaisesRegex(ValueError, "DHCE::HMSSHP"):
            select_stratified_cohorts(rows, seed="fixed")

    def test_live_source_identifiers_map_to_frozen_governance_strata(self):
        self.assertEqual(
            canonical_source_pair(
                "HQ-vDB01_HMSSHP_Prod::HQ-vDB01_DHCE_Prod"
            ),
            "DHCE::HMSSHP",
        )
        self.assertEqual(
            canonical_source_pair(
                "PHI-vDBUAT_HMSPhi_UAT::SHP-DB-UAT_HMSSHP_UAT"
            ),
            "PHI-UAT::SHP-UAT",
        )

    def test_full_165_passes_with_two_different_but_not_three(self):
        two = validation_gate(["Same"] * 163 + ["Different"] * 2)
        three = validation_gate(["Same"] * 162 + ["Different"] * 3)
        self.assertTrue(two["passed"])
        self.assertFalse(three["passed"])
        self.assertLess(three["wilson_95"][0], 0.95)

    def test_validation_attrition_and_unresolved_fail_closed(self):
        self.assertFalse(validation_gate(["Same"] * 149)["passed"])
        self.assertTrue(
            validation_gate(["Same"] * 150, stale_count=15)["passed"]
        )
        self.assertFalse(
            validation_gate(["Same"] * 165, unresolved_count=1)["passed"]
        )

    def test_components_distinguish_isolated_pairs_and_incomplete_graphs(self):
        rows = [
            {"name": "P1", "left_record": "A", "right_record": "B"},
            {"name": "P2", "left_record": "C", "right_record": "D"},
            {"name": "P3", "left_record": "D", "right_record": "E"},
        ]
        metadata = component_metadata(rows)
        self.assertTrue(metadata["P1"]["isolated_pair"])
        self.assertFalse(metadata["P2"]["complete_clique"])
        self.assertEqual(len(automatic_components(rows, maximum_size=2)), 1)

    def test_larger_complete_clique_support_is_bounded_by_configuration(self):
        rows = [
            {"name": "AB", "left_record": "A", "right_record": "B"},
            {"name": "AC", "left_record": "A", "right_record": "C"},
            {"name": "BC", "left_record": "B", "right_record": "C"},
        ]
        self.assertEqual(automatic_components(rows, maximum_size=2), ())
        self.assertEqual(len(automatic_components(rows, maximum_size=3)), 1)

    def test_shared_capacity_has_channel_floor_and_hard_cap(self):
        allocation = allocate_shared_capacity(
            {"Recommendation": 100, "Splink": 50}, capacity=20
        )
        self.assertEqual(sum(allocation.values()), 20)
        self.assertGreaterEqual(allocation["Recommendation"], 2)
        self.assertGreaterEqual(allocation["Splink"], 2)
        self.assertGreater(allocation["Recommendation"], allocation["Splink"])

    def test_shared_capacity_remainder_tracks_recent_unattended_volume(self):
        allocation = allocate_shared_capacity(
            {"Recommendation": 100, "Splink": 100},
            capacity=20,
            recent_unattended_volume={"Recommendation": 10, "Splink": 30},
        )
        self.assertEqual(sum(allocation.values()), 20)
        self.assertGreater(allocation["Splink"], allocation["Recommendation"])

    def test_governed_provenance_change_invalidates_authorization(self):
        frozen = {
            "queue_run": "Q1",
            "canary_run": "C1",
            "policy_snapshot_sha256": "P1",
            "model_versions_json": "M1",
            "runtime_versions_json": "R1",
            "source_scope_json": "S1",
            "frozen_cutoff": FROZEN_AUTOMATIC_CUTOFF,
        }
        current = dict(frozen, runtime_versions_json="R2")
        self.assertEqual(
            authorization_staleness(frozen, current), ("runtime_versions_json",)
        )


if __name__ == "__main__":
    unittest.main()
