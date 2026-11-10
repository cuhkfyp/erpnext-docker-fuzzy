import unittest

from fuzzy_matching.canary import (
    CanaryEdge,
    analyze_canary_edges,
    approved_high_rule,
    canonical_identity_groups,
    identity_partition_fingerprint,
    summarize_one_to_many_research,
)


def edge(left, right, left_source, right_source, source_pair="A::B"):
    return CanaryEdge(left, right, left_source, right_source, source_pair)


class CanaryGateTests(unittest.TestCase):
    def test_exact_policy_trusted_identifier_is_an_approved_high_route(self):
        self.assertTrue(
            approved_high_rule(
                ["trusted_global_id_exact:hkid", "name_conflict_warning"],
                {"hkid"},
            )
        )

    def test_untrusted_identifier_reason_is_not_an_approved_high_route(self):
        self.assertFalse(
            approved_high_rule(["trusted_global_id_exact:hkid"], {"hksr_num"})
        )

    def test_name_plus_independent_evidence_remains_an_approved_high_route(self):
        self.assertTrue(
            approved_high_rule(
                ["exact_name_plus_independent_evidence"],
                set(),
            )
        )

    def test_one_to_many_research_defaults_to_three_and_treats_missing_fields_as_neutral(self):
        rows = [
            {
                "cluster_fingerprint": "three",
                "cluster_size": 3,
                "left_record": "A",
                "right_record": "B",
                "left_source": "S1",
                "right_source": "S2",
                "reason_codes": ["exact_name_plus_independent_evidence"],
                "safety_reasons": ["one_to_many_source_conflict"],
                "component_review": "review-three",
            },
            {
                "cluster_fingerprint": "three",
                "cluster_size": 3,
                "left_record": "A",
                "right_record": "C",
                "left_source": "S1",
                "right_source": "S2",
                "reason_codes": ["exact_name_plus_independent_evidence"],
                "safety_reasons": ["one_to_many_source_conflict"],
                "component_review": "review-three",
            },
        ]
        result = summarize_one_to_many_research(rows)
        self.assertEqual(result["maximum_component_size"], 3)
        self.assertEqual(result["within_limit_component_count"], 1)
        self.assertEqual(result["unreviewed_research_candidate_count"], 1)
        self.assertEqual(
            result["neutral_missing_or_uncertain_fields"], ["birthday", "hkid"]
        )

    def test_one_to_many_research_size_is_configurable_and_trusted_conflict_is_hard(self):
        rows = [
            {
                "cluster_fingerprint": "four",
                "cluster_size": 4,
                "left_record": "A",
                "right_record": "B",
                "left_source": "S1",
                "right_source": "S2",
                "reason_codes": ["exact_name_plus_independent_evidence"],
                "safety_reasons": [
                    "one_to_many_source_conflict",
                    "transitive_trusted_identifier_conflict:hkid",
                ],
            },
            {
                "cluster_fingerprint": "four",
                "cluster_size": 4,
                "left_record": "A",
                "right_record": "C",
                "left_source": "S1",
                "right_source": "S2",
                "reason_codes": ["exact_name_plus_independent_evidence"],
                "safety_reasons": ["one_to_many_source_conflict"],
            },
            {
                "cluster_fingerprint": "four",
                "cluster_size": 4,
                "left_record": "A",
                "right_record": "D",
                "left_source": "S1",
                "right_source": "S2",
                "reason_codes": ["exact_name_plus_independent_evidence"],
                "safety_reasons": ["one_to_many_source_conflict"],
            },
        ]
        default_result = summarize_one_to_many_research(rows)
        configured_result = summarize_one_to_many_research(
            rows, maximum_component_size=4
        )
        self.assertEqual(default_result["above_limit_component_count"], 1)
        self.assertEqual(configured_result["within_limit_component_count"], 1)
        self.assertEqual(configured_result["hard_trusted_identifier_conflict_count"], 1)
        self.assertEqual(configured_result["unreviewed_research_candidate_count"], 0)

    def test_partial_review_pairs_close_transitively_and_keep_singletons(self):
        groups = canonical_identity_groups(
            ["D", "B", "A", "C"],
            [("A", "B"), ("B", "C")],
        )
        self.assertEqual(groups, (("A", "B", "C"), ("D",)))
        self.assertEqual(
            identity_partition_fingerprint(groups),
            identity_partition_fingerprint((("D",), ("C", "A", "B"))),
        )

    def test_partial_review_rejects_record_outside_component(self):
        with self.assertRaises(ValueError):
            canonical_identity_groups(["A", "B"], [("A", "C")])

    def test_safe_validated_pair_is_proposed(self):
        rows = [edge("A1", "B1", "A", "B")]
        records = {
            "A1": {"source": "A", "trusted_ids": {"hkid": "ID1"}},
            "B1": {"source": "B", "trusted_ids": {"hkid": "ID1"}},
        }
        decision = analyze_canary_edges(
            rows,
            records,
            validated_source_pairs={"A::B"},
        )[("A1", "B1")]
        self.assertEqual(decision.status, "Proposed")
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.cluster_size, 2)

    def test_one_to_many_source_marks_whole_component_exception(self):
        rows = [
            edge("A1", "B1", "A", "B"),
            edge("A1", "B2", "A", "B"),
        ]
        records = {
            "A1": {"source": "A", "trusted_ids": {}},
            "B1": {"source": "B", "trusted_ids": {}},
            "B2": {"source": "B", "trusted_ids": {}},
        }
        decisions = analyze_canary_edges(
            rows,
            records,
            validated_source_pairs={"A::B"},
        )
        self.assertTrue(all(item.status == "Exception" for item in decisions.values()))
        self.assertTrue(
            all("one_to_many_source_conflict" in item.reasons for item in decisions.values())
        )

    def test_transitive_identifier_and_model_conflicts_mark_component(self):
        rows = [
            edge("A1", "B1", "A", "B"),
            edge("B1", "C1", "B", "C", "B::C"),
        ]
        records = {
            "A1": {"source": "A", "trusted_ids": {"hkid": "ID1"}},
            "B1": {"source": "B", "trusted_ids": {}},
            "C1": {"source": "C", "trusted_ids": {"hkid": "ID2"}},
        }
        decisions = analyze_canary_edges(
            rows,
            records,
            validated_source_pairs={"A::B", "B::C"},
            conflicting_pairs={("C1", "A1")},
        )
        for decision in decisions.values():
            self.assertEqual(decision.status, "Exception")
            self.assertIn("transitive_trusted_identifier_conflict:hkid", decision.reasons)
            self.assertIn("transitive_model_conflict", decision.reasons)

    def test_pair_specific_source_coverage_and_staleness_are_gated(self):
        rows = [
            CanaryEdge(
                "A1",
                "B1",
                "A",
                "B",
                "A::B",
                approved_rule=False,
            )
        ]
        records = {
            "A1": {"source": "A", "trusted_ids": {}},
            "B1": {"source": "B", "trusted_ids": {}},
        }
        decision = analyze_canary_edges(
            rows,
            records,
            validated_source_pairs=set(),
            stale_record_ids={"B1"},
        )[("A1", "B1")]
        self.assertEqual(decision.status, "Exception")
        self.assertEqual(
            decision.reasons,
            ("stale_record", "unvalidated_high_rule", "unvalidated_source_pair"),
        )


if __name__ == "__main__":
    unittest.main()
