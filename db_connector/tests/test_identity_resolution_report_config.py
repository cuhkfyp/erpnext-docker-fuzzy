import json
import pathlib
import unittest


REPORT_CONFIG = (
    pathlib.Path(__file__).resolve().parents[1]
    / "db_connector"
    / "report"
    / "ccd_identity_resolution_register"
    / "ccd_identity_resolution_register.json"
)
REPORT_SCRIPT = REPORT_CONFIG.with_name("ccd_identity_resolution_register.js")


class IdentityResolutionReportConfigTests(unittest.TestCase):
    def test_report_runs_live_for_non_default_identity_state_filters(self):
        config = json.loads(REPORT_CONFIG.read_text(encoding="utf-8"))
        script = REPORT_SCRIPT.read_text(encoding="utf-8")

        self.assertEqual(config.get("prepared_report"), 0)
        self.assertIn("report.ignore_prepared_report = true", script)


if __name__ == "__main__":
    unittest.main()
