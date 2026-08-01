import unittest

from fuzzy_matching.blocking import generate_candidate_pairs
from fuzzy_matching.metrics import cohens_kappa, select_thresholds, wilson_interval
from fuzzy_matching.policy import MatchingPolicy, SourceProfile
from fuzzy_matching.security import mask_identifier, redact, safe_html


class BlockingTests(unittest.TestCase):
    def test_phone_block_recovers_cross_centre_pair_with_different_names(self):
        records = [
            {"record_id": "A", "source": "A", "eng_surname": "Example", "phone_num": "(+852) 1111 1111"},
            {"record_id": "B", "source": "B", "eng_surname": "Different", "phone_num": "11111111"},
            {"record_id": "C", "source": "A", "eng_surname": "Different", "phone_num": "11111111"},
        ]
        result = generate_candidate_pairs(records, MatchingPolicy())
        ids = {(item.left_id, item.right_id) for item in result.pairs}
        self.assertIn(("A", "B"), ids)
        self.assertNotIn(("A", "C"), ids)

    def test_same_source_pair_is_excluded(self):
        records = [
            {"record_id": "A", "source": "A", "phone_num": "11111111"},
            {"record_id": "B", "source": "A", "phone_num": "11111111"},
        ]
        self.assertFalse(generate_candidate_pairs(records, MatchingPolicy()).pairs)

    def test_unknown_identifier_finds_review_candidate_without_trusting_it(self):
        records = [
            {"record_id": "A", "source": "A", "hksr_num": "12345"},
            {"record_id": "B", "source": "B", "hksr_num": "12345"},
        ]
        profiles = {
            source: SourceProfile(source, {"hksr_num": "hksr_num"}, {"hksr_num": "unknown"})
            for source in ("A", "B")
        }
        result = generate_candidate_pairs(records, MatchingPolicy(source_profiles=profiles))
        self.assertEqual(len(result.pairs), 1)
        self.assertIn("unverified_id", result.pairs[0].blocking_routes)

    def test_oversized_block_metadata_does_not_expose_field_value(self):
        records = [
            {"record_id": "A", "source": "A", "phone_num": "11111111"},
            {"record_id": "B", "source": "B", "phone_num": "11111111"},
        ]
        policy = MatchingPolicy(max_block_size=1)
        result = generate_candidate_pairs(records, policy)
        self.assertTrue(result.skipped_blocks)
        self.assertNotIn("11111111", " ".join(result.skipped_blocks))


class MetricsTests(unittest.TestCase):
    def test_thresholds_require_high_sample_count(self):
        rows = [(True, 0.99)] * 5 + [(False, 0.2)] * 10
        selection = select_thresholds(rows, minimum_high_samples=30)
        self.assertIsNone(selection.high_threshold)
        self.assertIsNotNone(selection.review_threshold)

    def test_reviewer_agreement(self):
        self.assertEqual(cohens_kappa([("Same", "Same"), ("Different", "Different")]), 1.0)
        lower, upper = wilson_interval(95, 100)
        self.assertLess(lower, 0.95)
        self.assertGreater(upper, 0.95)


class SecurityTests(unittest.TestCase):
    def test_mask_and_escape(self):
        self.assertTrue(mask_identifier("A123456(3)").endswith("(3)"))
        self.assertEqual(safe_html('<script>alert("x")</script>'), "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;")
        self.assertEqual(redact({"hkid": "A123456(3)"})["hkid"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
