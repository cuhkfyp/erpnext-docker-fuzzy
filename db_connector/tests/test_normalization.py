import unittest

from fuzzy_matching import normalization as norm
from fuzzy_matching.comparators import compare_english, compare_phone
from fuzzy_matching.types import EvidenceLevel


class NormalizationTests(unittest.TestCase):
    def test_compact_english_ignores_spacing_and_case(self):
        evidence = compare_english("eng_firstname", "Test Person", "testperson")
        self.assertEqual(evidence.level, EvidenceLevel.EXACT)
        self.assertGreaterEqual(evidence.score, 0.95)

    def test_phone_country_code_normalization_is_exact_only(self):
        exact = compare_phone("phone", "(+852) 6123 4567", "61234567")
        partial = compare_phone("phone", "61234567", "61234568")
        self.assertEqual(exact.level, EvidenceLevel.EXACT)
        self.assertEqual(partial.level, EvidenceLevel.DISAGREE)
        self.assertEqual(partial.score, 0.0)

    def test_missing_phone_is_not_disagreement(self):
        evidence = compare_phone("phone", "61234567", "")
        self.assertEqual(evidence.level, EvidenceLevel.MISSING)
        self.assertFalse(evidence.available)

    def test_invalid_hong_kong_phone_placeholders_are_missing_from_new_strategy(self):
        for value in (
            "12345678",
            "23456789",
            "98765432",
            "11111111",
            "12121212",
            "12341234",
            "6123456",
            "+44 6123 4567",
        ):
            with self.subTest(value=value):
                self.assertEqual(norm.phone_tokens(value), ())

    def test_legacy_single_field_phone_normalization_remains_frozen(self):
        self.assertEqual(norm.phone("66666666"), "66666666")
        self.assertEqual(norm.matching_phone_tokens("66666666"), ("66666666",))
        self.assertEqual(norm.matching_phone_tokens(("66666666",)), ())

    def test_multi_phone_values_are_tokenized_and_compared_by_overlap(self):
        self.assertEqual(
            norm.phone_tokens("(+852) 9731 2175 / 93520882 / 12345678"),
            ("93520882", "97312175"),
        )
        evidence = compare_phone(
            "phone",
            ("25672313", "97312175/93520882"),
            ("97312175", "97882239"),
        )
        self.assertEqual(evidence.level, EvidenceLevel.EXACT)
        self.assertEqual(evidence.reason, "phone_token_exact")

    def test_hkid_structure_and_check_digit(self):
        self.assertTrue(norm.valid_hkid("A123456(3)"))
        self.assertFalse(norm.valid_hkid("A123456(4)"))


if __name__ == "__main__":
    unittest.main()
