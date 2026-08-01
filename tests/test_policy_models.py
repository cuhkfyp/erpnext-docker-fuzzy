import unittest

from fuzzy_matching.models import compare_all_models
from fuzzy_matching.policy import MatchingPolicy, SourceProfile
from fuzzy_matching.types import CandidatePair, EvidenceLevel, MatchTier


def pair(left="L", right="R"):
    return CandidatePair(left, right, "A::B", ("chi_full",))


class BossExamplesTests(unittest.TestCase):
    def setUp(self):
        self.policy = MatchingPolicy()

    def test_same_person_missing_phone_stays_review(self):
        left = {
            "record_id": "L",
            "source": "A",
            "chi_surname": "測",
            "chi_firstname": "試甲",
            "eng_surname": "Example",
            "eng_firstname": "Person Alpha",
            "phone_num": "11111111",
        }
        right = {
            "record_id": "R",
            "source": "B",
            "chi_surname": "測",
            "chi_firstname": "試甲",
            "eng_surname": "Example",
            "eng_firstname": "P A",
            "phone_num": "",
        }
        result = compare_all_models(pair(), left, right, self.policy)
        self.assertEqual(result.tiered_gated.tier, MatchTier.REVIEW)
        self.assertIn("insufficient_independent_evidence", result.tiered_gated.reasons)
        self.assertEqual(result.tiered_gated.evidence["phone"].level, EvidenceLevel.MISSING)

    def test_different_similar_person_cannot_reach_high(self):
        left = {
            "record_id": "L",
            "source": "A",
            "chi_surname": "測",
            "chi_firstname": "試甲",
            "eng_surname": "Example",
            "eng_firstname": "Alpha",
        }
        right = {
            "record_id": "R",
            "source": "B",
            "chi_surname": "測",
            "chi_firstname": "試乙",
            "eng_surname": "Example",
            "eng_firstname": "Alfa",
            "phone_num": "22222222",
        }
        result = compare_all_models(pair(), left, right, self.policy)
        self.assertNotEqual(result.tiered_gated.tier, MatchTier.HIGH)
        self.assertNotEqual(result.tiered_gated.evidence["chi_firstname"].level, EvidenceLevel.EXACT)

    def test_compact_name_true_case_ranks_above_false_case(self):
        true_left = {
            "record_id": "L",
            "source": "A",
            "chi_surname": "樣",
            "chi_firstname": "本甲",
            "eng_surname": "Sample",
            "eng_firstname": "Test Person",
        }
        true_right = {
            "record_id": "R",
            "source": "B",
            "chi_surname": "樣",
            "chi_firstname": "本甲",
            "eng_surname": "Sample",
            "eng_firstname": "testperson",
        }
        false_left = dict(true_left, chi_firstname="不同甲", eng_firstname="Alpha")
        false_right = dict(true_right, chi_firstname="不同乙", eng_firstname="Alfa")
        true_result = compare_all_models(pair(), true_left, true_right, self.policy)
        false_result = compare_all_models(pair(), false_left, false_right, self.policy)
        self.assertGreater(true_result.tiered_gated.score, false_result.tiered_gated.score)
        self.assertEqual(true_result.tiered_gated.tier, MatchTier.REVIEW)

    def test_exact_name_plus_birthday_reaches_high(self):
        left = {
            "record_id": "L", "source": "A", "chi_surname": "樣", "chi_firstname": "本甲",
            "birthday": "1980-01-02",
        }
        right = {
            "record_id": "R", "source": "B", "chi_surname": "樣", "chi_firstname": "本甲",
            "birthday": "1980/01/02",
        }
        result = compare_all_models(pair(), left, right, self.policy)
        self.assertEqual(result.tiered_gated.tier, MatchTier.HIGH)


class IdentifierPolicyTests(unittest.TestCase):
    def global_policy(self):
        profiles = {
            source: SourceProfile(source, {"hkid": "hkid"}, {"hkid": "global"})
            for source in ("A", "B")
        }
        return MatchingPolicy(
            source_profiles=profiles,
            trusted_global_identifiers=frozenset({"hkid"}),
        )

    def test_exact_trusted_id_is_high_with_name_warning(self):
        policy = self.global_policy()
        left = {"record_id": "L", "source": "A", "hkid": "A123456(3)", "eng_surname": "Example"}
        right = {"record_id": "R", "source": "B", "hkid": "A1234563", "eng_surname": "Different"}
        result = compare_all_models(pair(), left, right, policy)
        self.assertEqual(result.tiered_gated.tier, MatchTier.HIGH)
        self.assertIn("name_conflict_warning", result.tiered_gated.reasons)

    def test_trusted_id_conflict_has_two_safe_shadow_variants(self):
        policy = self.global_policy()
        left = {
            "record_id": "L", "source": "A", "hkid": "A123456(3)",
            "chi_surname": "樣", "chi_firstname": "本甲", "birthday": "1980-01-02",
        }
        right = {
            "record_id": "R", "source": "B", "hkid": "B987654(0)",
            "chi_surname": "樣", "chi_firstname": "本甲", "birthday": "1980-01-02",
        }
        result = compare_all_models(pair(), left, right, policy)
        self.assertEqual(result.tiered_gated.tier, MatchTier.CONFLICT)
        self.assertEqual(result.tiered_recoverable.tier, MatchTier.REVIEW)

    def test_unknown_local_id_is_not_trusted(self):
        policy = MatchingPolicy(trusted_global_identifiers=frozenset({"hkid"}))
        left = {"record_id": "L", "source": "A", "hkid": "A123456(3)"}
        right = {"record_id": "R", "source": "B", "hkid": "A123456(3)"}
        result = compare_all_models(pair(), left, right, policy)
        self.assertNotEqual(result.tiered_gated.tier, MatchTier.HIGH)
        self.assertEqual(result.tiered_gated.tier, MatchTier.REVIEW)
        self.assertIn("unverified_identifier_exact:hkid", result.tiered_gated.reasons)

    def test_trusted_name_does_not_override_local_identifier_scope(self):
        profiles = {
            source: SourceProfile(source, {"hkid": "hkid"}, {"hkid": "local"})
            for source in ("A", "B")
        }
        policy = MatchingPolicy(
            source_profiles=profiles,
            trusted_global_identifiers=frozenset({"hkid"}),
        )
        left = {"record_id": "L", "source": "A", "hkid": "A123456(3)"}
        right = {"record_id": "R", "source": "B", "hkid": "A123456(3)"}
        result = compare_all_models(pair(), left, right, policy)
        self.assertEqual(result.tiered_gated.tier, MatchTier.REVIEW)


if __name__ == "__main__":
    unittest.main()
