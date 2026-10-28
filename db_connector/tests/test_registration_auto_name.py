import unittest
from pathlib import Path


class RegistrationAutoNameScriptTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(__file__).resolve().parents[1]
        self.source = (
            self.app / "public/js/ccd_registration_auto_name.js"
        ).read_text(encoding="utf-8")

    def test_uses_the_established_stable_source_formats(self):
        self.assertIn(
            "[physical_hostname, database_name, table_name]",
            self.source,
        )
        self.assertIn('table_name.split(".").pop()', self.source)
        self.assertIn('table_name === "*** Customize ***"', self.source)
        self.assertNotIn("Math.random", self.source)
        self.assertNotIn("toUpperCase", self.source)

    def test_updates_after_any_source_field_changes(self):
        self.assertIn("physical_hostname(frm)", self.source)
        self.assertIn("db_database(frm)", self.source)
        self.assertIn("ccd_table(frm)", self.source)
        self.assertGreaterEqual(
            self.source.count("autofill_registration_name(frm, true)"), 3
        )

    def test_regenerates_immediately_before_validation(self):
        self.assertIn("validate(frm)", self.source)
        self.assertGreaterEqual(
            self.source.count("autofill_registration_name(frm, true)"), 4
        )

    def test_refresh_preserves_an_existing_manual_name(self):
        self.assertIn("(!overwrite && current_name)", self.source)
        self.assertIn("autofill_registration_name(frm, false)", self.source)


if __name__ == "__main__":
    unittest.main()
