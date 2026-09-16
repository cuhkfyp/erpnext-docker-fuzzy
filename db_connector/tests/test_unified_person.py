import ast
import pathlib
import unittest

from fuzzy_matching.unified_person import (
    format_unified_person_number,
    luhn_check_digit,
    parse_unified_person_number,
    plan_lineage_reassignment,
    recreated_lineage_person,
    source_lineage_key,
    valid_unified_person_number,
)


class UnifiedPersonNumberTests(unittest.TestCase):
    def test_format_and_parse_round_trip(self):
        for sequence in (1, 2, 97, 123456789, 999999999):
            number = format_unified_person_number(sequence)
            self.assertEqual(len(number), len("HKSR-U") + 10)
            self.assertEqual(parse_unified_person_number(number), sequence)
            self.assertTrue(valid_unified_person_number(number))

    def test_luhn_rejects_mutated_check_digit(self):
        number = format_unified_person_number(1)
        changed = number[:-1] + str((int(number[-1]) + 1) % 10)
        self.assertFalse(valid_unified_person_number(changed))
        with self.assertRaises(ValueError):
            parse_unified_person_number(changed)

    def test_sequence_zero_and_overflow_are_rejected(self):
        with self.assertRaises(ValueError):
            format_unified_person_number(0)
        with self.assertRaises(ValueError):
            format_unified_person_number(1_000_000_000)
        self.assertEqual(luhn_check_digit("1"), luhn_check_digit("000000001"))

    def test_source_lineage_survives_document_recreation(self):
        first = source_lineage_key("source-A", "person-123")
        recreated = source_lineage_key("source-A", "person-123")
        self.assertEqual(first, recreated)
        self.assertNotEqual(first, source_lineage_key("source-A", "person-124"))
        self.assertNotEqual(first, source_lineage_key("source-B", "person-123"))
        self.assertEqual(source_lineage_key("source-A", ""), "")

    def test_recreated_source_reuses_only_one_unambiguous_inactive_lineage(self):
        self.assertEqual(recreated_lineage_person(["U1", "U1"], []), "U1")
        self.assertEqual(recreated_lineage_person(["U1", "U2"], []), "")
        self.assertEqual(recreated_lineage_person(["U1"], ["still-active"]), "")
        self.assertEqual(recreated_lineage_person([], []), "")


class UnifiedPersonLineageTests(unittest.TestCase):
    def test_merge_keeps_oldest_and_aliases_other_numbers(self):
        plan = plan_lineage_reassignment(
            [["A", "B"]],
            {"U1": ["A"], "U2": ["B"]},
            {"U1": 1, "U2": 2},
        )
        self.assertEqual(plan.cluster_people, {0: "U1"})
        self.assertEqual(plan.alias_cluster, {"U2": 0})

    def test_split_restores_unambiguous_original_lineage(self):
        plan = plan_lineage_reassignment(
            [["A"], ["B"]],
            {"U1": ["A", "B"], "U2": ["B"]},
            {"U1": 1, "U2": 2},
        )
        self.assertEqual(plan.cluster_people, {0: "U1", 1: "U2"})
        self.assertEqual(plan.alias_cluster, {})

    def test_split_without_original_lineage_uses_survivor_and_new_number(self):
        plan = plan_lineage_reassignment(
            [["A"], ["B"]],
            {"U1": ["A", "B"]},
            {"U1": 1},
        )
        self.assertEqual(plan.cluster_people, {0: "U1"})
        self.assertNotIn(1, plan.cluster_people)

    def test_merge_and_split_plan_is_order_independent(self):
        first = plan_lineage_reassignment(
            [["C"], ["B", "A"]],
            {"U3": ["C"], "U2": ["B"], "U1": ["A"]},
            {"U1": 1, "U2": 2, "U3": 3},
        )
        second = plan_lineage_reassignment(
            [["A", "B"], ["C"]],
            {"U1": ["A"], "U3": ["C"], "U2": ["B"]},
            {"U3": 3, "U2": 2, "U1": 1},
        )
        self.assertEqual(first, second)


class UnifiedPersonStorageContractTests(unittest.TestCase):
    def test_service_never_writes_a_unified_number_to_ccd_master(self):
        source_path = pathlib.Path(__file__).resolve().parents[1] / "api_unified_person.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_set_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"set_value", "set_single_value"} or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "CCD Master":
                forbidden_set_calls.append(node.lineno)
        self.assertEqual(forbidden_set_calls, [])
        self.assertNotIn("UPDATE `tabCCD Master`", source)


if __name__ == "__main__":
    unittest.main()
