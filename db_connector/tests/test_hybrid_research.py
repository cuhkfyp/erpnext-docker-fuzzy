import unittest

from fuzzy_matching.hybrid_research import (
    classify_corroboration,
    deterministic_stratified_sample,
    lower_score_band,
    quality_aware_birthday,
)
from fuzzy_matching.types import Evidence, EvidenceLevel


def item(attribute, level):
    return Evidence(
        attribute,
        level,
        1.0 if level == EvidenceLevel.EXACT else 0.0,
        True,
        True,
        "test",
    )


class HybridResearchTests(unittest.TestCase):
    def _evidence(self, **levels):
        defaults = {
            "chi_surname": EvidenceLevel.EXACT,
            "chi_firstname": EvidenceLevel.EXACT,
            "eng_surname": EvidenceLevel.MISSING,
            "eng_firstname": EvidenceLevel.MISSING,
            "phone": EvidenceLevel.MISSING,
            "email": EvidenceLevel.MISSING,
            "hkid": EvidenceLevel.MISSING,
            "hksr_num": EvidenceLevel.MISSING,
        }
        defaults.update(levels)
        return {
            attribute: item(attribute, level)
            for attribute, level in defaults.items()
        }

    def test_identical_placeholder_birthday_is_positive_unconfirmed(self):
        result = quality_aware_birthday(
            "1980-01-01", "1980-01-01", "No", None
        )
        self.assertEqual(result["status"], "birthday_exact_unconfirmed")
        self.assertTrue(result["positive"])
        self.assertFalse(result["conflict"])

    def test_uncertain_birthday_mismatch_is_neutral(self):
        result = quality_aware_birthday(
            "1961-10-06", "1959-01-01", "Yes", "No"
        )
        self.assertEqual(result["status"], "birthday_mismatch_uncertain_neutral")
        self.assertFalse(result["positive"])
        self.assertFalse(result["conflict"])

    def test_confirmed_birthday_mismatch_is_conflict(self):
        result = quality_aware_birthday(
            "1961-10-06", "1959-01-01", "Yes", "Yes"
        )
        self.assertEqual(result["status"], "birthday_mismatch_confirmed")
        self.assertTrue(result["conflict"])

    def test_exact_name_and_unconfirmed_equal_birthday_is_research_route(self):
        result = classify_corroboration(
            self._evidence(),
            left_birthday="1980-01-01",
            right_birthday="1980-01-01",
            left_birthday_key="No",
            right_birthday_key=None,
        )
        self.assertTrue(result["selected_for_research"])
        self.assertIn(
            "exact_name+birthday_exact_unconfirmed", result["routes"]
        )

    def test_other_corroboration_routes_are_supported(self):
        result = classify_corroboration(
            self._evidence(
                phone=EvidenceLevel.EXACT,
                email=EvidenceLevel.EXACT,
                hksr_num=EvidenceLevel.EXACT,
            ),
            left_birthday=None,
            right_birthday=None,
            left_birthday_key=None,
            right_birthday_key=None,
        )
        self.assertTrue(result["selected_for_research"])
        self.assertIn("exact_name+phone_exact", result["routes"])
        self.assertIn("exact_name+email_exact", result["routes"])
        self.assertIn(
            "exact_name+unverified_identifier_exact", result["routes"]
        )

    def test_partial_name_identifier_and_equal_birthday_form_research_route(self):
        result = classify_corroboration(
            self._evidence(
                chi_firstname=EvidenceLevel.MISSING,
                eng_surname=EvidenceLevel.MISSING,
                eng_firstname=EvidenceLevel.EXACT,
                hksr_num=EvidenceLevel.EXACT,
            ),
            left_birthday="1980-01-01",
            right_birthday="1980-01-01",
            left_birthday_key="No",
            right_birthday_key=None,
        )
        self.assertTrue(result["selected_for_research"])
        self.assertIn(
            "name_support+unverified_identifier+secondary_exact",
            result["routes"],
        )

    def test_trusted_identifier_conflict_fails_closed(self):
        result = classify_corroboration(
            self._evidence(hkid=EvidenceLevel.DISAGREE),
            left_birthday="1980-01-01",
            right_birthday="1980-01-01",
            left_birthday_key="No",
            right_birthday_key="No",
            trusted_identifiers={"hkid"},
        )
        self.assertFalse(result["selected_for_research"])
        self.assertIn("trusted_identifier_conflict:hkid", result["hard_conflicts"])

    def test_score_bands_keep_ultra_high_separate(self):
        cutoff = 0.999148140
        self.assertEqual(
            lower_score_band(0.999077019, cutoff), "0.999_to_ultra_high"
        )
        self.assertEqual(
            lower_score_band(cutoff, cutoff), "at_or_above_ultra_high"
        )

    def _sample_row(
        self,
        name,
        route,
        band="0.95_to_0.99",
        source="HMSSHP::SHP-UAT",
        birthday="birthday_exact_unconfirmed",
    ):
        return {
            "name": name,
            "routes": [route],
            "score_band": band,
            "source_pair": source,
            "birthday_status": birthday,
        }

    def test_exploratory_sample_is_order_independent_and_exact_size(self):
        rows = [
            self._sample_row(
                f"major-{index:03d}",
                "major",
                band=("0.99_to_0.999" if index % 2 else "0.95_to_0.99"),
            )
            for index in range(120)
        ]
        first = deterministic_stratified_sample(rows, 50, "a" * 64)
        second = deterministic_stratified_sample(
            list(reversed(rows)), 50, "a" * 64
        )
        self.assertEqual(first["selected_size"], 50)
        self.assertEqual(
            [row["name"] for row in first["rows"]],
            [row["name"] for row in second["rows"]],
        )
        self.assertEqual(len({row["name"] for row in first["rows"]}), 50)

    def test_exploratory_sample_includes_rare_routes_in_full(self):
        rows = [
            self._sample_row(f"rare-{index}", "rare") for index in range(4)
        ] + [
            self._sample_row(f"major-{index:03d}", "major")
            for index in range(100)
        ]
        result = deterministic_stratified_sample(rows, 30, "b" * 64)
        names = {row["name"] for row in result["rows"]}
        self.assertEqual(
            {f"rare-{index}" for index in range(4)}.intersection(names),
            {f"rare-{index}" for index in range(4)},
        )
        self.assertEqual(result["rare_routes_included_in_full"], ["rare"])

    def test_exploratory_sample_represents_routes_and_strata(self):
        rows = []
        for route in ("route-a", "route-b"):
            for index in range(60):
                rows.append(
                    self._sample_row(
                        f"{route}-{index:03d}",
                        route,
                        band=(
                            "0.999_to_ultra_high"
                            if index % 3 == 0
                            else "0.95_to_0.99"
                        ),
                        source=(
                            "DHCE::HMSSHP"
                            if index % 5 == 0
                            else "HMSSHP::SHP-UAT"
                        ),
                    )
                )
        result = deterministic_stratified_sample(rows, 50, "c" * 64)
        selected = result["rows"]
        self.assertEqual({row["primary_route"] for row in selected}, {"route-a", "route-b"})
        self.assertEqual(
            {row["score_band"] for row in selected},
            {"0.999_to_ultra_high", "0.95_to_0.99"},
        )
        self.assertEqual(
            {row["source_pair"] for row in selected},
            {"DHCE::HMSSHP", "HMSSHP::SHP-UAT"},
        )


if __name__ == "__main__":
    unittest.main()
