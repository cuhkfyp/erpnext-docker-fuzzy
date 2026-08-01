import importlib
import sys
import types
import unittest


class LegacyAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_frappe = types.SimpleNamespace(
            whitelist=lambda: (lambda function: function),
            log_error=lambda *args, **kwargs: None,
            safe_eval=lambda expression: eval(expression, {"__builtins__": {}}, {}),
        )
        sys.modules.setdefault("frappe", fake_frappe)
        cls.module = importlib.import_module("api_ccd_fuzzy")

    def test_empty_formula_compiler_has_consistent_three_value_contract(self):
        precompute, pair_fn, slots = self.module.compile_formula("")
        self.assertEqual(precompute({}), {})
        self.assertEqual(pair_fn({}, {}), (0.0, False, {}))
        self.assertEqual(slots, [])

    def test_audit_html_escapes_record_values_and_expands_formula(self):
        formula = '(@EnglishMatch("eng_firstname")*1.0)>0.5'
        precompute, pair_fn, slots = self.module.compile_formula(formula)
        source = precompute({"eng_firstname": '<script>alert("x")</script>'})
        candidate = precompute({"eng_firstname": '<script>alert("x")</script>'})
        score, matched, scores = pair_fn(source, candidate)
        html = self.module.building_html_audit_table(slots, scores, source, candidate, formula)
        self.assertTrue(matched)
        self.assertEqual(score, 1.0)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("@EnglishMatch", html.split("Formula Evaluation Trail:", 1)[1])

    def test_formula_compiler_rejects_calls_and_attribute_access(self):
        with self.assertRaises(ValueError):
            self.module.compile_formula('().__class__.__base__.__subclasses__()>0')


if __name__ == "__main__":
    unittest.main()
