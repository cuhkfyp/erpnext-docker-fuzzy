import unittest

from fuzzy_matching.identity import (
    build_materialization_plan,
    complete_hkid_conflicts,
    expected_identity_fingerprints,
    fingerprint_scoped_exclusion_conflicts,
    identity_fingerprint,
    normalize_partition,
    snapshot_modified_conflicts,
    validate_component_atomic_selection,
)
from fuzzy_matching.policy import MatchingPolicy, SourceProfile


def policy():
    return MatchingPolicy(
        version="pilot-1.6",
        source_profiles={
            "A": SourceProfile(
                source="A",
                field_map={"hkid": "hkid", "phone": "phone", "birthday": "dob"},
                identifier_scope={"hkid": "global"},
            ),
            "B": SourceProfile(
                source="B",
                field_map={"hkid": "hkid", "phone": "phone", "birthday": "dob"},
                identifier_scope={"hkid": "global"},
            ),
        },
        trusted_global_identifiers=frozenset({"hkid"}),
    )


class IdentityFingerprintTests(unittest.TestCase):
    def test_missing_frozen_fingerprint_is_not_the_text_none(self):
        self.assertEqual(
            expected_identity_fingerprints(
                [("A", None), ("B", ""), ("C", "fp-c")]
            ),
            {"C": "fp-c"},
        )

    def test_conflicting_frozen_fingerprints_fail_closed(self):
        with self.assertRaises(ValueError):
            expected_identity_fingerprints([("A", "fp-1"), ("A", "fp-2")])

    def test_snapshot_modified_conflicts_include_missing_and_changed_records(self):
        self.assertEqual(
            snapshot_modified_conflicts(
                {"A": "2026-01-01", "B": "2026-01-02", "C": "2026-01-03"},
                {"A": "2026-01-01", "B": "changed"},
            ),
            ("B", "C"),
        )

    def test_administrative_change_does_not_change_fingerprint(self):
        first = {
            "source": "A",
            "hkid": "A123456(3)",
            "phone": "9123 4567",
            "dob": "2000-01-02",
            "modified": "2026-01-01",
            "notes": "first",
        }
        second = {**first, "modified": "2026-08-20", "notes": "changed"}
        self.assertEqual(identity_fingerprint(first, policy()), identity_fingerprint(second, policy()))

    def test_governed_identity_change_changes_fingerprint(self):
        first = {"source": "A", "phone": "91234567", "dob": "2000-01-02"}
        second = {**first, "phone": "92345678"}
        self.assertNotEqual(identity_fingerprint(first, policy()), identity_fingerprint(second, policy()))

    def test_complete_valid_hkid_conflict_fails_but_partial_does_not(self):
        records = {
            "A": {"source": "A", "hkid": "A123456(3)"},
            "B": {"source": "B", "hkid": "C123456(9)"},
        }
        self.assertEqual(len(complete_hkid_conflicts((("A", "B"),), records, policy())), 1)
        records["B"]["hkid"] = "C123***"
        self.assertEqual(complete_hkid_conflicts((("A", "B"),), records, policy()), ())


class IdentityPlanTests(unittest.TestCase):
    def test_partition_must_be_complete_and_non_overlapping(self):
        self.assertEqual(
            normalize_partition(["A", "B", "C"], [["B", "A"], ["C"]]),
            (("A", "B"), ("C",)),
        )
        with self.assertRaises(ValueError):
            normalize_partition(["A", "B"], [["A"], ["A", "B"]])
        with self.assertRaises(ValueError):
            normalize_partition(["A", "B"], [["A"]])

    def test_component_selector_cannot_split_a_component(self):
        all_edges = [
            ("C1", "A", "B"),
            ("C1", "B", "C"),
            ("C2", "D", "E"),
        ]
        with self.assertRaises(ValueError):
            validate_component_atomic_selection(all_edges, [("C1", "A", "B")])
        self.assertEqual(
            validate_component_atomic_selection(all_edges, all_edges[:2]),
            ("C1",),
        )

    def test_materialization_key_is_stable_and_order_independent(self):
        first = build_materialization_plan(
            origin="Tiered Evidence",
            origin_document="RUN-1:C1",
            policy_version="pilot-1.6",
            record_ids=["C", "A", "B"],
            groups=[["B", "A"], ["C"]],
            exclusions=[("C", "A")],
        )
        second = build_materialization_plan(
            origin="Tiered Evidence",
            origin_document="RUN-1:C1",
            policy_version="pilot-1.6",
            record_ids=["A", "B", "C"],
            groups=[["C"], ["A", "B"]],
            exclusions=[("A", "C")],
        )
        self.assertEqual(first.idempotency_key, second.idempotency_key)

    def test_exclusion_applies_only_to_same_unchanged_fingerprints(self):
        fingerprints = {"A": "fp-a", "B": "fp-b", "C": "fp-c"}
        exclusions = [("A", "B", "fp-a", "fp-b")]
        self.assertEqual(
            fingerprint_scoped_exclusion_conflicts(
                [["A", "B"], ["C"]], fingerprints, exclusions
            ),
            (("A", "B"),),
        )
        self.assertEqual(
            fingerprint_scoped_exclusion_conflicts(
                [["A"], ["B", "C"]], fingerprints, exclusions
            ),
            (),
        )
        self.assertEqual(
            fingerprint_scoped_exclusion_conflicts(
                [["A", "B"], ["C"]], {**fingerprints, "A": "changed"}, exclusions
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
