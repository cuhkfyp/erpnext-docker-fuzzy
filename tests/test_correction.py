import unittest

from fuzzy_matching.correction import (
    correction_key,
    exclusions_for_partition,
    normalize_partition,
    stable_payload_fingerprint,
)


class IdentityCorrectionHelperTests(unittest.TestCase):
    def test_partition_is_complete_deterministic_and_non_overlapping(self):
        self.assertEqual(
            normalize_partition(["C", "A", "B"], [["C"], ["B", "A"]]),
            (("A", "B"), ("C",)),
        )
        with self.assertRaisesRegex(ValueError, "complete scope"):
            normalize_partition(["A", "B", "C"], [["A", "B"]])
        with self.assertRaisesRegex(ValueError, "more than one"):
            normalize_partition(["A", "B"], [["A", "B"], ["B"]])
        with self.assertRaisesRegex(ValueError, "non-empty CCD record ID"):
            normalize_partition(["A", "B"], [["A"], [None]])

    def test_cross_partition_exclusions_are_complete(self):
        self.assertEqual(
            exclusions_for_partition((("A", "B"), ("C",), ("D",))),
            (("A", "C"), ("A", "D"), ("B", "C"), ("B", "D"), ("C", "D")),
        )

    def test_fingerprints_and_keys_are_order_independent(self):
        self.assertEqual(
            stable_payload_fingerprint({"b": 2, "a": [1]}),
            stable_payload_fingerprint({"a": [1], "b": 2}),
        )
        self.assertEqual(
            correction_key("D1", "S1", [["B", "A"], ["C"]]),
            correction_key("D1", "S1", [["C"], ["A", "B"]]),
        )


if __name__ == "__main__":
    unittest.main()
