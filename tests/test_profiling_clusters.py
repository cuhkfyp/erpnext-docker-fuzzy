import unittest

from fuzzy_matching.clusters import inconsistent_pairs
from fuzzy_matching.policy import MatchingPolicy
from fuzzy_matching.profiling import profile_attributes
from fuzzy_matching.types import CandidatePair, EvaluationResult, MatchTier, ModelResult


def model(name, tier):
    return ModelResult(name, 1.0 if tier == MatchTier.HIGH else 0.5, tier)


def result(left, right, tier):
    candidate = CandidatePair(left, right, "A::B", ("test",))
    baseline = model("baseline", MatchTier.REVIEW)
    tiered = model("tiered", tier)
    return EvaluationResult(candidate, baseline, tiered, tiered)


class ProfilingTests(unittest.TestCase):
    def test_reports_coverage_duplicates_and_cross_source_overlap(self):
        rows = [
            {"source": "A", "hkid": "A123456(3)", "phone_num": "91234567"},
            {"source": "A", "hkid": "A123456(3)", "phone_num": ""},
            {"source": "B", "hkid": "A1234563", "phone_num": "91234567"},
        ]
        report = profile_attributes(rows, MatchingPolicy())
        self.assertEqual(report["sources"]["A"]["attributes"]["hkid"]["duplicate_values"], 1)
        self.assertEqual(report["sources"]["A"]["attributes"]["phone"]["coverage"], 0.5)
        self.assertEqual(report["cross_source_distinct_overlap"]["hkid"]["A::B"], 1)


class ClusterTests(unittest.TestCase):
    def test_flags_connected_group_containing_identifier_conflict(self):
        rows = [
            result("A", "B", MatchTier.HIGH),
            result("B", "C", MatchTier.REVIEW),
            result("A", "C", MatchTier.CONFLICT),
        ]
        flagged = inconsistent_pairs(rows)
        self.assertEqual(flagged, {("A", "B"), ("A", "C"), ("B", "C")})


if __name__ == "__main__":
    unittest.main()
