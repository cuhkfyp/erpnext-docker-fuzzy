import types
import unittest
from unittest.mock import MagicMock, patch

from db_connector import api_splink_automation as service


class SplinkAutomationServiceTests(unittest.TestCase):
    def test_replacement_queue_requires_exact_fitted_model_runtime_and_scope(self):
        def queue(name, *, runtime_seed=19, source="DHCE"):
            return types.SimpleNamespace(
                name=name,
                status="Ready",
                policy_snapshot_sha256="policy-sha",
                threshold_evaluation_run="threshold-run",
                splink_adapter_version="splink-v1",
                policy_snapshot_json=(
                    '{"source_profiles":{"' + source + '":{}}}'
                ),
                summary_json=(
                    '{"splink_dependencies":{"splink":"4.0"},'
                    '"blocking_version":"blocking-v1",'
                    '"random_match_prior":0.001,'
                    f'"frozen_u_random_seed":{runtime_seed}' + "}"
                ),
            )

        frozen = queue("frozen")
        choices = {
            "wrong-runtime": queue("wrong-runtime", runtime_seed=20),
            "wrong-scope": queue("wrong-scope", source="HMSSHP"),
            "exact": queue("exact"),
        }
        rows = [types.SimpleNamespace(name=name) for name in choices]
        with patch.object(service.frappe, "get_all", return_value=rows) as get_all, patch.object(
            service.frappe, "get_doc", side_effect=lambda _doctype, name: choices[name]
        ):
            replacement = service._matching_ready_queue(
                types.SimpleNamespace(name="validation-run"), frozen
            )
        self.assertEqual(replacement.name, "exact")
        self.assertEqual(
            get_all.call_args.kwargs["filters"],
            {
                "status": "Ready",
                "policy_snapshot_sha256": "policy-sha",
                "threshold_evaluation_run": "threshold-run",
                "splink_adapter_version": "splink-v1",
            },
        )

    def test_scheduled_candidates_use_current_queue_and_exclude_any_human_label(self):
        rows = [
            {
                "name": "clean",
                "stale": 0,
                "automation_reserved": 0,
                "automation_batch": None,
                "automation_status": "Optional Review",
            },
            {
                "name": "reviewed",
                "stale": 0,
                "automation_reserved": 0,
                "automation_batch": None,
                "automation_status": "Optional Review",
            },
            {
                "name": "reserved",
                "stale": 0,
                "automation_reserved": 1,
                "automation_batch": None,
                "automation_status": "Reserved Holdout",
            },
        ]
        run = types.SimpleNamespace(frozen_automatic_cutoff=service.FROZEN_AUTOMATIC_CUTOFF)
        with patch.object(service, "_queue_rows", return_value=rows) as queue_rows, patch.object(
            service, "_reviewed_candidate_names", return_value={"reviewed"}
        ):
            selected = service._scheduled_candidates(run, "replacement-ready")
        self.assertEqual([row["name"] for row in selected], ["clean"])
        queue_rows.assert_called_once_with(
            "replacement-ready", minimum_score=service.FROZEN_AUTOMATIC_CUTOFF
        )

    def test_scheduled_batch_reserves_candidates_with_complete_provenance(self):
        run = types.SimpleNamespace(
            name="validation-run",
            validation_queue_run="frozen-queue",
            matching_policy="pilot-1.7",
            frozen_automatic_cutoff=service.FROZEN_AUTOMATIC_CUTOFF,
            authorization_fingerprint="authorization",
        )
        planned = [
            {
                "component_fingerprint": "component",
                "component_size": 2,
                "complete_clique": True,
                "candidate_names": ["candidate"],
                "record_ids": ["A", "B"],
                "eligible": True,
                "error": "",
            }
        ]
        batch = MagicMock()
        batch.name = "batch"
        database = MagicMock()
        database.get_value.return_value = None
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_doc", return_value=batch
        ) as get_doc, patch.object(
            service.frappe, "session", types.SimpleNamespace(user="test@example.com")
        ), patch.object(
            service, "now_datetime", return_value="2026-10-10 00:00:00"
        ):
            result = service._create_batch(
                batch_type="Scheduled Splink",
                run=run,
                planned=planned,
                queue_run="replacement-ready",
            )
        self.assertIs(result, batch)
        inserted = get_doc.call_args.args[0]
        self.assertEqual(inserted["queue_run"], "replacement-ready")
        candidate_values = database.set_value.call_args.args[2]
        self.assertEqual(candidate_values["automation_validation_run"], run.name)
        self.assertEqual(candidate_values["automation_cohort"], "Scheduled Splink")
        self.assertEqual(candidate_values["automation_reserved"], 1)
        self.assertEqual(candidate_values["automation_component_fingerprint"], "component")


if __name__ == "__main__":
    unittest.main()
