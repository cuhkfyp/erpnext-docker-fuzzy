import json
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_connector import api_hybrid_exploratory_review as service


class HybridExploratoryReviewTests(unittest.TestCase):
    def _queue(self):
        return types.SimpleNamespace(
            name="queue-1",
            policy_version="pilot-1.8",
            policy_snapshot_json='{"version":"pilot-1.8"}',
        )

    def test_request_fingerprint_is_stable_and_floor_specific(self):
        queue = self._queue()
        first = service._selection_fingerprint(queue, 0.99914814, 0.95)
        second = service._selection_fingerprint(queue, 0.99914814, 0.95)
        changed = service._selection_fingerprint(queue, 0.99914814, 0.99)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed)

    def test_progress_counts_exploratory_outcomes_without_candidate_writes(self):
        rows = [
            types.SimpleNamespace(
                status="Reviewed", exploratory_label="Same", count=7
            ),
            types.SimpleNamespace(
                status="Reviewed", exploratory_label="Different", count=2
            ),
            types.SimpleNamespace(
                status="Reviewed", exploratory_label="Unsure", count=1
            ),
            types.SimpleNamespace(status="Stale", exploratory_label="", count=1),
            types.SimpleNamespace(
                status="Unreviewed", exploratory_label="", count=89
            ),
        ]
        database = MagicMock()
        database.get_value.return_value = 100
        with patch.object(service.frappe, "get_all", return_value=rows), patch.object(
            service.frappe, "db", database
        ):
            result = service._refresh_batch_progress("batch-1", "reviewer")
        self.assertEqual(
            result,
            {
                "reviewed": 10,
                "remaining": 89,
                "same": 7,
                "different": 2,
                "unsure": 1,
                "stale": 1,
            },
        )
        written = database.set_value.call_args.args
        self.assertEqual(written[0], service.BATCH_DOCTYPE)
        self.assertEqual(written[1], "batch-1")
        self.assertNotIn("status", written[2])

    def test_future_formal_selection_excludes_every_frozen_sample_member(self):
        batches = ["batch-1"]
        candidates = ["candidate-1", "candidate-2"]
        with patch.object(
            service.frappe,
            "get_all",
            side_effect=[batches, candidates],
        ) as get_all:
            result = service.hybrid_exploratory_candidate_names("queue-1")
        self.assertEqual(result, set(candidates))
        self.assertEqual(get_all.call_args_list[1].kwargs["pluck"], "review_candidate")

    def test_desk_exposes_persistent_batch_and_review_controls(self):
        app = Path(__file__).resolve().parents[1]
        settings_source = (
            app
            / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.js"
        ).read_text(encoding="utf-8")
        batch_source = (
            app
            / "db_connector/doctype/ccd_hybrid_exploratory_batch/ccd_hybrid_exploratory_batch.js"
        ).read_text(encoding="utf-8")
        batch_list_source = (
            app
            / "db_connector/doctype/ccd_hybrid_exploratory_batch/ccd_hybrid_exploratory_batch_list.js"
        ).read_text(encoding="utf-8")
        item_source = (
            app
            / "db_connector/doctype/ccd_hybrid_exploratory_item/ccd_hybrid_exploratory_item.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Create Persistent Exploratory Review Batch", settings_source)
        self.assertIn("start_hybrid_exploratory_batch", settings_source)
        self.assertIn("Review Next Unreviewed", batch_source)
        self.assertIn("frm.disable_save()", batch_source)
        self.assertIn("Create / Reopen Governed Batch", batch_source)
        self.assertIn("set_primary_action", batch_list_source)
        self.assertIn("start_hybrid_exploratory_batch", batch_list_source)
        self.assertIn("submit_hybrid_exploratory_label", item_source)

    def test_reviewer_permissions_are_read_only(self):
        app = Path(__file__).resolve().parents[1]
        for doctype in (
            "ccd_hybrid_exploratory_batch",
            "ccd_hybrid_exploratory_item",
        ):
            schema = json.loads(
                (
                    app
                    / "db_connector/doctype"
                    / doctype
                    / f"{doctype}.json"
                ).read_text(encoding="utf-8")
            )
            for permission in schema["permissions"]:
                if permission["role"] in {
                    "CCD Match Reviewer",
                    "CCD Match Sensitive Reviewer",
                }:
                    self.assertEqual(permission.get("write", 0), 0)
                    self.assertEqual(permission.get("create", 0), 0)
                    self.assertEqual(permission.get("delete", 0), 0)


if __name__ == "__main__":
    unittest.main()
