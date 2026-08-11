import unittest

from fuzzy_matching.blocking import generate_candidate_pairs
from fuzzy_matching.metrics import cohens_kappa, select_thresholds, wilson_interval
from fuzzy_matching.policy import MatchingPolicy, SourceProfile
from fuzzy_matching.security import mask_identifier, redact, safe_html


class BlockingTests(unittest.TestCase):
    def test_phone_block_recovers_cross_centre_pair_with_different_names(self):
        records = [
            {"record_id": "A", "source": "A", "eng_surname": "Example", "phone_num": "(+852) 6123 4567"},
            {"record_id": "B", "source": "B", "eng_surname": "Different", "phone_num": "61234567"},
            {"record_id": "C", "source": "A", "eng_surname": "Different", "phone_num": "61234567"},
        ]
        result = generate_candidate_pairs(records, MatchingPolicy())
        ids = {(item.left_id, item.right_id) for item in result.pairs}
        self.assertIn(("A", "B"), ids)
        self.assertNotIn(("A", "C"), ids)

    def test_same_source_pair_is_excluded(self):
        records = [
            {"record_id": "A", "source": "A", "phone_num": "61234567"},
            {"record_id": "B", "source": "A", "phone_num": "61234567"},
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

    def test_only_complete_valid_hkid_uses_global_identifier_block(self):
        profiles = {
            source: SourceProfile(source, {"hkid": "hkid"}, {"hkid": "global"})
            for source in ("A", "B")
        }
        policy = MatchingPolicy(
            source_profiles=profiles,
            trusted_global_identifiers=frozenset({"hkid"}),
        )
        complete = [
            {"record_id": "A", "source": "A", "hkid": "A123456(3)"},
            {"record_id": "B", "source": "B", "hkid": "A1234563"},
        ]
        partial = [
            {"record_id": "C", "source": "A", "hkid": "A123***/X"},
            {"record_id": "D", "source": "B", "hkid": "A123***/X"},
        ]
        complete_result = generate_candidate_pairs(complete, policy)
        partial_result = generate_candidate_pairs(partial, policy)
        self.assertIn("global_id", complete_result.pairs[0].blocking_routes)
        self.assertNotIn("global_id", partial_result.pairs[0].blocking_routes)
        self.assertIn("unverified_id", partial_result.pairs[0].blocking_routes)

    def test_candidate_cap_prioritizes_exact_contact_over_name_block(self):
        records = [
            {"record_id": "N1", "source": "A", "eng_surname": "Example", "eng_firstname": "Alpha"},
            {"record_id": "N2", "source": "B", "eng_surname": "Example", "eng_firstname": "Alfred"},
            {"record_id": "P1", "source": "A", "phone_num": "61234567"},
            {"record_id": "P2", "source": "B", "phone_num": "61234567"},
        ]
        policy = MatchingPolicy(max_candidate_pairs=1)
        first = generate_candidate_pairs(records, policy)
        second = generate_candidate_pairs(list(reversed(records)), policy)
        self.assertEqual(first.pairs, second.pairs)
        self.assertEqual((first.pairs[0].left_id, first.pairs[0].right_id), ("P1", "P2"))
        self.assertIn("phone", first.pairs[0].blocking_routes)

    def test_chinese_name_prefix_is_narrower_than_surname_initial(self):
        records = [
            {"record_id": "A", "source": "A", "chi_surname": "陳", "chi_firstname": "大文"},
            {"record_id": "B", "source": "B", "chi_surname": "陳", "chi_firstname": "大明"},
            {"record_id": "C", "source": "B", "chi_surname": "陳", "chi_firstname": "小文"},
        ]
        result = generate_candidate_pairs(records, MatchingPolicy())
        ids = {(item.left_id, item.right_id) for item in result.pairs}
        self.assertIn(("A", "B"), ids)
        self.assertNotIn(("A", "C"), ids)

    def test_bounded_chinese_variant_routes_recover_homophone_and_transposition(self):
        records = [
            {"record_id": "A", "source": "A", "chi_surname": "王", "chi_firstname": "小明"},
            {"record_id": "B", "source": "B", "chi_surname": "王", "chi_firstname": "小鸣"},
            {"record_id": "C", "source": "B", "chi_surname": "王", "chi_firstname": "明小"},
        ]
        result = generate_candidate_pairs(records, MatchingPolicy())
        by_pair = {(item.left_id, item.right_id): item.blocking_routes for item in result.pairs}
        self.assertIn("chi_pinyin_full", by_pair[("A", "B")])
        self.assertIn("chi_given_sorted", by_pair[("A", "C")])

    def test_broad_name_cap_prioritizes_best_match_for_sparse_endpoint(self):
        records = [
            {"record_id": "A1", "source": "A", "chi_surname": "陳", "chi_firstname": "大文強"},
            {"record_id": "A2", "source": "A", "chi_surname": "陳", "chi_firstname": "大東海"},
            {"record_id": "B1", "source": "B", "chi_surname": "陳", "chi_firstname": "大文康"},
        ]
        result = generate_candidate_pairs(records, MatchingPolicy(max_candidate_pairs=1))
        reversed_result = generate_candidate_pairs(
            list(reversed(records)), MatchingPolicy(max_candidate_pairs=1)
        )
        self.assertEqual(result.pairs, reversed_result.pairs)
        self.assertTrue(result.truncated)
        self.assertEqual((result.pairs[0].left_id, result.pairs[0].right_id), ("A1", "B1"))
        self.assertIn("chi_name_prefix", result.pairs[0].blocking_routes)

    def test_oversized_block_metadata_does_not_expose_field_value(self):
        records = [
            {"record_id": "A", "source": "A", "phone_num": "61234567"},
            {"record_id": "B", "source": "B", "phone_num": "61234567"},
        ]
        policy = MatchingPolicy(max_block_size=1)
        result = generate_candidate_pairs(records, policy)
        self.assertTrue(result.skipped_blocks)
        self.assertNotIn("61234567", " ".join(result.skipped_blocks))


class MetricsTests(unittest.TestCase):
    def test_thresholds_require_high_sample_count(self):
        rows = [(True, 0.99)] * 5 + [(False, 0.2)] * 10
        selection = select_thresholds(rows, minimum_high_samples=30)
        self.assertIsNone(selection.high_threshold)
        self.assertIsNotNone(selection.review_threshold)

    def test_reviewer_agreement(self):
        self.assertEqual(cohens_kappa([("Same", "Same"), ("Different", "Different")]), 1.0)
        self.assertIsNone(cohens_kappa([("Different", "Different")] * 100))
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
