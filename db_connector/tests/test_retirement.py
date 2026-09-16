import unittest

from fuzzy_matching.retirement import (
    group_retirement_targets,
    stable_scope_fingerprint,
    stale_materialization_status,
    stale_review_status,
)


class RetirementHelperTests(unittest.TestCase):
    def test_scope_fingerprint_is_order_independent_for_mappings(self):
        self.assertEqual(
            stable_scope_fingerprint({"b": 2, "a": ["x"]}),
            stable_scope_fingerprint({"a": ["x"], "b": 2}),
        )

    def test_completed_human_reviews_and_applied_history_are_preserved(self):
        self.assertEqual(stale_review_status("Agreed"), "Agreed")
        self.assertEqual(stale_review_status("Adjudicated"), "Adjudicated")
        self.assertEqual(stale_review_status("Unreviewed"), "Stale")
        self.assertEqual(stale_materialization_status("Applied"), "Applied")
        self.assertEqual(stale_materialization_status("Corrected"), "Corrected")
        self.assertEqual(stale_materialization_status("Pending"), "Stale")

    def test_group_with_only_one_survivor_ends_every_current_membership(self):
        plan = group_retirement_targets(
            [
                {
                    "name": "M1",
                    "identity_group": "G1",
                    "ccd_master": "retired",
                    "status": "Active",
                },
                {
                    "name": "M2",
                    "identity_group": "G1",
                    "ccd_master": "survivor",
                    "status": "Needs Revalidation",
                },
            ],
            ["survivor"],
        )
        self.assertEqual(plan["end_groups"], ("G1",))
        self.assertEqual(plan["end_memberships"], ("M1", "M2"))
        self.assertEqual(plan["revalidate_memberships"], ())
        self.assertEqual(plan["survivor_counts"], {"G1": 1})

    def test_group_with_two_survivors_is_revalidated_component_wide(self):
        plan = group_retirement_targets(
            [
                {
                    "name": "M1",
                    "identity_group": "G1",
                    "ccd_master": "retired",
                    "status": "Active",
                },
                {
                    "name": "M2",
                    "identity_group": "G1",
                    "ccd_master": "A",
                    "status": "Active",
                },
                {
                    "name": "M3",
                    "identity_group": "G1",
                    "ccd_master": "B",
                    "status": "Active",
                },
                {
                    "name": "old",
                    "identity_group": "G1",
                    "ccd_master": "retired-old",
                    "status": "Ended",
                },
            ],
            ["A", "B"],
        )
        self.assertEqual(plan["revalidate_groups"], ("G1",))
        self.assertEqual(plan["end_memberships"], ("M1",))
        self.assertEqual(plan["revalidate_memberships"], ("M2", "M3"))
        self.assertEqual(plan["end_groups"], ())


if __name__ == "__main__":
    unittest.main()
