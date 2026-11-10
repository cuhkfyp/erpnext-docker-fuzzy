import json
import unittest
from pathlib import Path

from db_connector import api_automatic_overlap as service
from db_connector.api_identity_overlap import (
    automatic_overlap_conflicts,
    automatic_overlap_error_is_eligible,
)


class AutomaticOverlapClassificationTests(unittest.TestCase):
    def test_only_structural_overlap_errors_are_eligible(self):
        error = (
            "Identity safety checks failed: existing_group_overlap, "
            "partial_existing_identity_group"
        )
        self.assertEqual(
            automatic_overlap_conflicts(error),
            ("existing_group_overlap", "partial_existing_identity_group"),
        )
        self.assertTrue(automatic_overlap_error_is_eligible(error))
        self.assertFalse(
            automatic_overlap_error_is_eligible(
                error + ", complete_hkid_conflict"
            )
        )
        self.assertFalse(automatic_overlap_error_is_eligible("birthday_conflict"))

    def test_selection_is_tiered_first_bounded_and_scope_disjoint(self):
        def row(channel, name, records, hard=(), controls=()):
            return {
                "channel": channel,
                "seed_doctype": "Seed",
                "seed_document": name,
                "error": "",
                "preview": {
                    "hard_conflicts": list(hard),
                    "control_blockers": list(controls),
                    "records": [{"record_id": value} for value in records],
                },
            }

        inventory = [
            row("Tiered High", "tiered-a", ("A", "B")),
            row("Tiered High", "tiered-overlap", ("B", "C")),
            row("Ultra-High Splink", "splink-a", ("D", "E")),
            row("Ultra-High Splink", "splink-hard", ("F", "G"), ("complete_hkid_conflict",)),
        ]
        selected, excluded = service._select(
            inventory, 2, require_controls=False
        )
        self.assertEqual(
            [item["seed_document"] for item in selected],
            ["tiered-a", "splink-a"],
        )
        self.assertEqual(
            excluded["overlaps_component_already_selected_this_cycle"], 1
        )
        self.assertEqual(excluded["complete_hkid_conflict"], 1)

    def test_operational_controls_fail_closed_only_at_run_selection(self):
        inventory = [
            {
                "channel": "Tiered High",
                "seed_doctype": "Seed",
                "seed_document": "A",
                "error": "",
                "preview": {
                    "hard_conflicts": [],
                    "control_blockers": ["automatic_overlap_disabled"],
                    "records": [{"record_id": "A"}, {"record_id": "B"}],
                },
            }
        ]
        preview_selected, _ = service._select(
            inventory, 1, require_controls=False
        )
        run_selected, reasons = service._select(
            inventory, 1, require_controls=True
        )
        self.assertEqual(len(preview_selected), 1)
        self.assertEqual(run_selected, [])
        self.assertEqual(reasons["automatic_overlap_disabled"], 1)


class AutomaticOverlapDeskContractTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(__file__).resolve().parents[1]

    def test_controls_are_default_off_and_persistent(self):
        schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.json"
            ).read_text(encoding="utf-8")
        )
        fields = {field["fieldname"]: field for field in schema["fields"]}
        self.assertEqual(fields["automatic_overlap_enabled"]["default"], "0")
        self.assertEqual(
            fields["last_automatic_overlap_batch"]["options"],
            "CCD Automatic Overlap Batch",
        )
        self.assertIn("automatic_overlap_qc_percent", fields)
        batch_schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_automatic_overlap_batch/ccd_automatic_overlap_batch.json"
            ).read_text(encoding="utf-8")
        )
        batch_fields = {field["fieldname"] for field in batch_schema["fields"]}
        self.assertTrue(
            {
                "selection_manifest_sha256",
                "authorization_event",
                "items",
                "qc_unassigned_count",
            }.issubset(batch_fields)
        )

    def test_desk_exposes_preview_control_run_and_live_progress(self):
        source = (
            self.app
            / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Enable Automatic Overlap Recovery", source)
        self.assertIn("Preview Complete-Group Recovery", source)
        self.assertIn("Run One Overlap Recovery Cycle Now", source)
        self.assertIn("get_automatic_overlap_batch_progress", source)
        self.assertIn("risk-accepted unassigned", source)

    def test_new_origins_have_governed_provenance(self):
        decision_schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_identity_decision/ccd_identity_decision.json"
            ).read_text(encoding="utf-8")
        )
        origin = next(
            field for field in decision_schema["fields"] if field["fieldname"] == "origin"
        )
        self.assertIn("Tiered Overlap Automated", origin["options"].splitlines())
        self.assertIn("Splink Overlap Automated", origin["options"].splitlines())

    def test_splink_recovery_enters_sampling_pool_not_mandatory_backlog(self):
        source = (self.app / "api_identity_overlap.py").read_text(encoding="utf-8")
        self.assertIn('"Sampling Eligible" if satisfied else None', source)
        self.assertNotIn(
            '"automated_qc_review_status": "Unreviewed" if satisfied else None',
            source,
        )


if __name__ == "__main__":
    unittest.main()
