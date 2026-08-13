import unittest

from fuzzy_matching.splink_adapter import (
    _null_missing_comparison_values,
    score_requested_pairs,
)


class SplinkAdapterTests(unittest.TestCase):
    def test_missing_comparison_values_become_null_not_exact_empty_text(self):
        original = {
            "record_id": "R1",
            "source": "A",
            "chi_full": "",
            "eng_full": "Example Person",
            "birthday": "",
            "phone": "",
            "email": None,
            "global_id": "",
        }
        converted = _null_missing_comparison_values([original])[0]

        self.assertIsNone(converted["chi_full"])
        self.assertEqual(converted["eng_full"], "Example Person")
        self.assertIsNone(converted["birthday"])
        self.assertIsNone(converted["phone"])
        self.assertIsNone(converted["email"])
        self.assertEqual(converted["global_id"], "")
        self.assertEqual(original["chi_full"], "")

    def test_empty_requested_pair_set_does_not_train_or_require_splink(self):
        self.assertEqual(
            score_requested_pairs([], [], [], minimum_probability=0.5),
            [],
        )


if __name__ == "__main__":
    unittest.main()
