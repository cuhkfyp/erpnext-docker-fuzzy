import unittest

from fuzzy_matching.blocking import (
    generate_candidate_pairs,
    generate_deterministic_high_candidate_pairs,
)
from fuzzy_matching.metrics import cohens_kappa, select_thresholds, wilson_interval
from fuzzy_matching.models import compare_all_models
from fuzzy_matching.policy import MatchingPolicy, SourceProfile
from fuzzy_matching.security import mask_identifier, redact, safe_html
from fuzzy_matching.types import CandidatePair, MatchTier


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

    def test_dob_surname_nominates_nearest_full_name_per_other_source(self):
        records = [
            {
                "record_id": "A1",
                "source": "A",
                "eng_surname": "Example",
                "eng_firstname": "Alpha",
                "birthday": "1980-01-02",
            },
            {
                "record_id": "A2",
                "source": "A",
                "eng_surname": "Example",
                "eng_firstname": "Zulu",
                "birthday": "1980-01-02",
            },
            {
                "record_id": "B1",
                "source": "B",
                "eng_surname": "Example",
                "eng_firstname": "Alphi",
                "birthday": "1980-01-02",
            },
            {
                "record_id": "B2",
                "source": "B",
                "eng_surname": "Example",
                "eng_firstname": "Zulu",
                "birthday": "1980-01-02",
            },
        ]
        first = generate_candidate_pairs(records, MatchingPolicy())
        second = generate_candidate_pairs(list(reversed(records)), MatchingPolicy())
        self.assertEqual(first.pairs, second.pairs)
        by_pair = {
            (item.left_id, item.right_id): item.blocking_routes
            for item in first.pairs
        }
        self.assertIn("dob_surname", by_pair[("A1", "B1")])
        self.assertIn("dob_surname", by_pair[("A2", "B2")])
        self.assertNotIn("dob_surname", by_pair.get(("A1", "B2"), ()))
        self.assertNotIn("dob_surname", by_pair.get(("A2", "B1"), ()))

    def test_dob_surname_nomination_retains_stronger_route_provenance(self):
        records = [
            {
                "record_id": "A",
                "source": "A",
                "eng_surname": "Example",
                "eng_firstname": "Alpha",
                "birthday": "1980-01-02",
                "phone_num": "61234567",
            },
            {
                "record_id": "B",
                "source": "B",
                "eng_surname": "Example",
                "eng_firstname": "Alpha",
                "birthday": "1980-01-02",
                "phone_num": "+852 6123 4567",
            },
        ]
        result = generate_candidate_pairs(records, MatchingPolicy())
        self.assertFalse(result.truncated)
        self.assertEqual(len(result.pairs), 1)
        self.assertIn("phone", result.pairs[0].blocking_routes)
        self.assertIn("dob_surname", result.pairs[0].blocking_routes)

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

    def test_high_candidate_routes_are_complete_for_every_deterministic_high(self):
        records = [
            {"record_id": "G1", "source": "A", "hksr_num": "GLOBAL-001"},
            {"record_id": "G2", "source": "B", "hksr_num": "GLOBAL-001"},
            {
                "record_id": "P1",
                "source": "A",
                "eng_surname": "Phone",
                "eng_firstname": "Person",
                "phone_num": "61234567",
            },
            {
                "record_id": "P2",
                "source": "B",
                "eng_surname": "Phone",
                "eng_firstname": "Person",
                "phone_num": "+852 6123 4567",
            },
            {
                "record_id": "E1",
                "source": "A",
                "chi_surname": "陳",
                "chi_firstname": "電郵",
                "email": "person@example.test",
            },
            {
                "record_id": "E2",
                "source": "C",
                "chi_surname": "陳",
                "chi_firstname": "電郵",
                "email": "PERSON@example.test",
            },
            {
                "record_id": "C1",
                "source": "A",
                "chi_surname": "黃",
                "chi_firstname": "大明",
                "birthday": "1980-01-02",
            },
            {
                "record_id": "C2",
                "source": "B",
                "chi_surname": "黃",
                "chi_firstname": "大明",
                "birthday": "02/01/1980",
            },
            {
                "record_id": "N1",
                "source": "A",
                "eng_surname": "O' Neil",
                "eng_firstname": "John Paul",
                "birthday": "1990/03/04",
            },
            {
                "record_id": "N2",
                "source": "C",
                "eng_surname": "ONEIL",
                "eng_firstname": "JohnPaul",
                "birthday": "1990-03-04",
            },
            {
                "record_id": "D1",
                "source": "B",
                "eng_surname": "Unrelated",
                "eng_firstname": "Record",
                "birthday": "2001-01-01",
            },
        ]
        profiles = {
            source: SourceProfile(
                source,
                {"hksr_num": "hksr_num"},
                {"hksr_num": "global"},
            )
            for source in ("A", "B", "C")
        }
        policy = MatchingPolicy(
            source_profiles=profiles,
            trusted_global_identifiers=frozenset({"hksr_num"}),
        )
        generated = generate_deterministic_high_candidate_pairs(records, policy)
        generated_keys = {
            (pair.left_id, pair.right_id) for pair in generated.pairs
        }
        by_id = {record["record_id"]: record for record in records}
        high_keys = set()
        for left_index, left in enumerate(records):
            for right in records[left_index + 1 :]:
                if left["source"] == right["source"]:
                    continue
                left_id, right_id = sorted((left["record_id"], right["record_id"]))
                pair = CandidatePair(
                    left_id,
                    right_id,
                    "::".join(sorted((left["source"], right["source"]))),
                    (),
                )
                result = compare_all_models(
                    pair,
                    by_id[left_id],
                    by_id[right_id],
                    policy,
                )
                if result.tiered_gated.tier == MatchTier.HIGH:
                    high_keys.add((left_id, right_id))
        self.assertTrue(high_keys)
        self.assertTrue(high_keys.issubset(generated_keys))
        self.assertFalse(generated.truncated)
        self.assertFalse(generated.skipped_blocks)

    def test_high_candidate_generation_reports_cap_and_oversized_blocks(self):
        records = [
            {"record_id": "A", "source": "A", "phone_num": "61234567"},
            {"record_id": "B", "source": "B", "phone_num": "61234567"},
            {"record_id": "C", "source": "C", "phone_num": "61234567"},
        ]
        capped = generate_deterministic_high_candidate_pairs(
            records,
            MatchingPolicy(max_candidate_pairs=1),
        )
        self.assertEqual(len(capped.pairs), 1)
        self.assertTrue(capped.truncated)

        skipped = generate_deterministic_high_candidate_pairs(
            records,
            MatchingPolicy(max_block_size=2),
        )
        self.assertFalse(skipped.pairs)
        self.assertTrue(skipped.skipped_blocks)
        self.assertNotIn("61234567", " ".join(skipped.skipped_blocks))


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
