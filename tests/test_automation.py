import unittest
from datetime import datetime, timedelta

from fuzzy_matching.automation import (
    cadence_due,
    current_shared_group,
    deterministic_qc_selection,
    rolling_qc_summary,
)


class AutomationHelperTests(unittest.TestCase):
    def test_qc_different_targets_only_the_current_shared_group(self):
        memberships = [
            {"ccd_master": "A", "identity_group": "G1"},
            {"ccd_master": "B", "identity_group": "G1"},
            {"ccd_master": "C", "identity_group": "G1"},
        ]
        self.assertEqual(current_shared_group(memberships, "A", "B"), "G1")

    def test_separate_current_groups_are_not_suspended(self):
        memberships = [
            {"ccd_master": "A", "identity_group": "G1"},
            {"ccd_master": "B", "identity_group": "G2"},
        ]
        self.assertIsNone(current_shared_group(memberships, "A", "B"))

    def test_multiple_shared_groups_fail_closed(self):
        memberships = [
            {"ccd_master": "A", "identity_group": "G1"},
            {"ccd_master": "B", "identity_group": "G1"},
            {"ccd_master": "A", "identity_group": "G2"},
            {"ccd_master": "B", "identity_group": "G2"},
        ]
        with self.assertRaisesRegex(ValueError, "more than one"):
            current_shared_group(memberships, "A", "B")

    def test_continuous_qc_selection_is_deterministic_and_bounded(self):
        rows = [
            {"name": "R3", "recommendation_key": "K3"},
            {"name": "R1", "recommendation_key": "K1"},
            {"name": "R2", "recommendation_key": "K2"},
        ]
        first = deterministic_qc_selection("RUN", rows, 2)
        second = deterministic_qc_selection("RUN", reversed(rows), 2)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(set(first)), 2)

    def test_rolling_window_uses_latest_comparable_results(self):
        result = rolling_qc_summary(
            ["Different", "Same", "Ignored", "Same", "Same"], 3
        )
        self.assertEqual(result["window_finalized"], 3)
        self.assertEqual(result["same"], 3)
        self.assertEqual(result["different"], 0)
        self.assertTrue(result["window_complete"])

    def test_cadence_is_due_when_missing_or_expired(self):
        now = datetime(2026, 8, 28, 12, 0, 0)
        self.assertTrue(cadence_due(None, now))
        self.assertTrue(cadence_due(now - timedelta(seconds=1), now))
        self.assertFalse(cadence_due(now + timedelta(seconds=1), now))


if __name__ == "__main__":
    unittest.main()
