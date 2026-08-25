import unittest

from fuzzy_matching.overlap import (
    conflicting_different_pairs,
    constraint_partition,
    partition_splits_groups,
    structural_overlap_only,
)


class IdentityOverlapHelperTests(unittest.TestCase):
    def test_same_constraints_are_transitive_and_complete(self):
        self.assertEqual(
            constraint_partition(
                ["A", "B", "C", "D"],
                [["A", "B"], ["B", "C"]],
            ),
            (("A", "B", "C"), ("D",)),
        )

    def test_different_constraint_inside_same_group_is_reported(self):
        self.assertEqual(
            conflicting_different_pairs(
                (("A", "B", "C"), ("D",)),
                (("A", "C"), ("A", "D")),
            ),
            (("A", "C"),),
        )

    def test_prior_group_split_is_detected(self):
        self.assertEqual(
            partition_splits_groups(
                (("A", "B"), ("C",), ("D",)),
                (("A", "B", "C"), ("D",)),
            ),
            (("A", "B", "C"),),
        )
        self.assertEqual(
            partition_splits_groups(
                (("A", "B", "C", "D"),),
                (("A", "B", "C"),),
            ),
            (),
        )

    def test_structural_overlap_classification_is_fail_closed(self):
        self.assertTrue(structural_overlap_only(["partial_existing_identity_group"]))
        self.assertTrue(
            structural_overlap_only(
                ["conflicting_active_identity_groups", "active_human_exclusion"]
            )
        )
        self.assertFalse(structural_overlap_only([]))
        self.assertFalse(
            structural_overlap_only(
                ["partial_existing_identity_group"], stale=True
            )
        )
        self.assertFalse(
            structural_overlap_only(
                ["partial_existing_identity_group", "complete_hkid_conflict"]
            )
        )


if __name__ == "__main__":
    unittest.main()
