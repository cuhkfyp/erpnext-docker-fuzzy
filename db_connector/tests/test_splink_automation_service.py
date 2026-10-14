import json
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_connector import api_identity_automation as tiered_automation
from db_connector import api_splink_automation as service


class SplinkAutomationServiceTests(unittest.TestCase):
    def setUp(self):
        flags = patch.object(
            service.frappe.local,
            "flags",
            types.SimpleNamespace(in_test=True),
            create=True,
        )
        flags.start()
        self.addCleanup(flags.stop)

    def test_system_manager_can_read_frozen_governance_fields(self):
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "db_connector"
            / "doctype"
            / "ccd_match_evaluation_run"
            / "ccd_match_evaluation_run.json"
        )
        permissions = json.loads(schema_path.read_text())["permissions"]
        self.assertTrue(
            any(
                row.get("role") == "System Manager"
                and row.get("permlevel") == 2
                and row.get("read") == 1
                for row in permissions
            )
        )
        self.assertFalse(
            any(
                row.get("role") in {
                    "CCD Match Reviewer",
                    "CCD Match Sensitive Reviewer",
                }
                and row.get("permlevel") == 2
                for row in permissions
            )
        )

    def test_identity_event_schema_accepts_retry_audit_event(self):
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "db_connector"
            / "doctype"
            / "ccd_identity_event"
            / "ccd_identity_event.json"
        )
        schema = json.loads(schema_path.read_text())
        event_field = next(
            field for field in schema["fields"] if field["fieldname"] == "event_type"
        )
        self.assertIn("Retry", event_field["options"].splitlines())

    def test_splink_retry_replaces_the_previous_failed_queue_job(self):
        batch = types.SimpleNamespace(
            name="failed-batch",
            status="Failed",
            validation_run="validation-run",
            authorization_fingerprint="authorization",
            batch_type="Validation Label Activation",
        )
        run = types.SimpleNamespace(authorization_fingerprint="authorization")
        database = MagicMock()
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_roles", return_value=["System Manager"]
        ), patch.object(
            service.frappe, "get_doc", side_effect=[batch, run]
        ), patch.object(
            service.frappe, "enqueue"
        ) as enqueue, patch.object(
            service.frappe, "session", types.SimpleNamespace(user="manager@example.com")
        ), patch.object(
            service, "_lock_rows"
        ), patch.object(
            service, "_authorization_current", return_value=(True, [])
        ), patch.object(
            service, "_append_event", return_value="retry-event"
        ):
            result = service.retry_splink_batch(
                batch.name, "materialization enabled", batch.name
            )
        self.assertEqual(result, {"batch": batch.name, "status": "Queued"})
        self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
        self.assertEqual(
            enqueue.call_args.kwargs["job_id"], "ccd-splink-batch-failed-batch"
        )
        database.commit.assert_called_once_with()

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

    def test_shared_capacity_skips_optional_query_when_mandatory_work_fills_cap(self):
        settings = types.SimpleNamespace(
            automatic_qc_assignment_enabled=1,
            qc_sla_days=14,
        )
        recommendations = [
            types.SimpleNamespace(name=f"recommendation-{index}", recommendation_key=str(index))
            for index in range(2)
        ]
        splink = [
            types.SimpleNamespace(name=f"splink-{index}", pair_key=str(index))
            for index in range(18)
        ]
        database = MagicMock()
        database.count.side_effect = [46, 0, 0]
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_single", return_value=settings
        ), patch.object(
            service.frappe, "get_all"
        ) as get_all, patch.object(
            service, "_weekly_assigned_count", return_value=0
        ), patch.object(
            service, "_mandatory_unassigned", return_value=(recommendations, splink)
        ), patch.object(
            service, "_hybrid_sampling_eligible", return_value=[]
        ), patch.object(
            service, "_week_start", return_value="2026-10-12"
        ), patch.object(
            service, "now_datetime", return_value="2026-10-12 12:54:55"
        ), patch.object(
            service, "add_days", return_value="2026-10-26 12:54:55"
        ), patch.object(
            service,
            "_advance_shared_recommendation_cadence",
            return_value={"status": "Not Configured", "advanced": False},
        ):
            result = service.run_shared_review_capacity()

        self.assertEqual(result["assigned"], 20)
        self.assertEqual(len(result["recommendation_qc"]), 2)
        self.assertEqual(len(result["splink_qc"]), 18)
        self.assertEqual(result["hybrid_qc"], [])
        self.assertEqual(result["hybrid_not_selected"], [])
        self.assertEqual(result["optional_splink"], [])
        self.assertEqual(result["remaining_capacity"], 0)
        get_all.assert_not_called()
        self.assertEqual(database.set_value.call_count, 20)
        database.commit.assert_called_once_with()

    def test_due_shared_cadence_advances_when_weekly_capacity_is_exhausted(self):
        settings = types.SimpleNamespace(
            automatic_tiered_canary="canary-run",
            qc_assignment_interval_days=7,
        )
        run = types.SimpleNamespace(
            name="canary-run",
            qc_next_assignment_at=datetime(2026, 10, 11, 0, 1, 44),
            qc_assignment_cycle_count=4,
        )
        current = datetime(2026, 10, 13, 10, 30, 0)
        expected_next = datetime(2026, 10, 20, 10, 30, 0)
        database = MagicMock()
        database.exists.return_value = True
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_doc", return_value=run
        ), patch.object(service, "_lock_rows") as lock_rows, patch.object(
            service, "_append_event", return_value="cadence-event"
        ) as append_event:
            result = service._advance_shared_recommendation_cadence(
                settings,
                now=current,
                recommendation_names=(),
                already_used=48,
            )

        self.assertTrue(result["advanced"])
        self.assertEqual(result["status"], "Advanced")
        self.assertEqual(result["assigned"], 0)
        self.assertEqual(result["next_assignment_at"], expected_next)
        self.assertEqual(result["assignment_cycle_count"], 5)
        lock_rows.assert_called_once_with(service.CANARY_RUN_DOCTYPE, ("canary-run",))
        database.set_value.assert_called_once_with(
            service.CANARY_RUN_DOCTYPE,
            "canary-run",
            {
                "qc_last_assignment_at": current,
                "qc_next_assignment_at": expected_next,
                "qc_assignment_cycle_count": 5,
            },
            update_modified=False,
        )
        metadata = append_event.call_args.kwargs["metadata"]
        self.assertTrue(metadata["capacity_exhausted"])
        self.assertEqual(metadata["assigned"], 0)

    def test_shared_capacity_refreshes_due_cadence_with_no_weekly_slots(self):
        settings = types.SimpleNamespace(
            automatic_qc_assignment_enabled=1,
            automatic_tiered_canary="canary-run",
            qc_assignment_interval_days=7,
            qc_sla_days=14,
        )
        run = types.SimpleNamespace(
            name="canary-run",
            qc_next_assignment_at=datetime(2026, 10, 11, 0, 1, 44),
            qc_assignment_cycle_count=4,
        )
        current = datetime(2026, 10, 13, 10, 30, 0)
        database = MagicMock()
        database.exists.return_value = True
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_single", return_value=settings
        ), patch.object(
            service.frappe, "get_all"
        ) as get_all, patch.object(
            service.frappe, "get_doc", return_value=run
        ), patch.object(
            service, "_weekly_assigned_count", return_value=48
        ), patch.object(
            service, "_mandatory_unassigned", return_value=([], [])
        ), patch.object(
            service, "_hybrid_sampling_eligible", return_value=[]
        ), patch.object(
            service, "_week_start", return_value=datetime(2026, 10, 12)
        ), patch.object(
            service, "now_datetime", return_value=current
        ), patch.object(service, "_lock_rows"), patch.object(
            service, "_append_event", return_value="cadence-event"
        ):
            result = service.run_shared_review_capacity()

        self.assertEqual(result["status"], "No Work")
        self.assertEqual(result["assigned"], 0)
        self.assertEqual(result["remaining_capacity"], 0)
        self.assertTrue(result["recommendation_cadence"]["advanced"])
        self.assertEqual(
            result["recommendation_cadence"]["next_assignment_at"],
            datetime(2026, 10, 20, 10, 30, 0),
        )
        get_all.assert_not_called()
        database.commit.assert_called_once_with()

    def test_exhausted_week_closes_hybrid_population_without_qc_backlog(self):
        settings = types.SimpleNamespace(
            automatic_qc_assignment_enabled=1,
            automatic_tiered_canary="",
            qc_assignment_interval_days=7,
            qc_sla_days=14,
        )
        hybrid = [
            types.SimpleNamespace(
                name=f"hybrid-{index}",
                pair_key=f"pair-{index}",
                automation_validation_run="hybrid-run",
                automated_applied_at="2026-10-14 17:46:59",
            )
            for index in range(3)
        ]
        database = MagicMock()
        database.count.side_effect = [3, 3, 0]
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_single", return_value=settings
        ), patch.object(
            service.frappe, "get_all"
        ) as get_all, patch.object(
            service, "_weekly_assigned_count", return_value=48
        ), patch.object(
            service, "_mandatory_unassigned", return_value=([], [])
        ), patch.object(
            service, "_hybrid_sampling_eligible", return_value=hybrid
        ), patch.object(
            service, "_week_start", return_value=datetime(2026, 10, 12)
        ), patch.object(
            service, "now_datetime", return_value=datetime(2026, 10, 14, 18, 0)
        ), patch.object(
            service,
            "_advance_shared_recommendation_cadence",
            return_value={"status": "Not Configured", "advanced": False},
        ):
            result = service.run_shared_review_capacity()

        self.assertEqual(result["assigned"], 0)
        self.assertEqual(result["hybrid_qc"], [])
        self.assertEqual(
            result["hybrid_not_selected"],
            ["hybrid-0", "hybrid-1", "hybrid-2"],
        )
        self.assertEqual(database.set_value.call_count, 3)
        for call in database.set_value.call_args_list:
            values = call.args[2]
            self.assertEqual(values["automated_qc_review_status"], "Not Selected")
            self.assertIsNone(values["automated_qc_assigned_at"])
            self.assertIsNone(values["automated_qc_due_at"])
        get_all.assert_not_called()
        database.commit.assert_called_once_with()

    def test_hybrid_sample_is_bounded_and_remainder_is_not_selected(self):
        settings = types.SimpleNamespace(
            automatic_qc_assignment_enabled=1,
            automatic_tiered_canary="",
            qc_assignment_interval_days=7,
            qc_sla_days=14,
        )
        recommendations = [
            types.SimpleNamespace(name=f"recommendation-{index}", recommendation_key=str(index))
            for index in range(2)
        ]
        hybrid = [
            types.SimpleNamespace(
                name=f"hybrid-{index}",
                pair_key=f"pair-{index}",
                automation_validation_run="hybrid-run",
                automated_applied_at="2026-10-14 17:46:59",
            )
            for index in range(3)
        ]
        database = MagicMock()
        database.count.side_effect = [3, 3, 2]
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_single", return_value=settings
        ), patch.object(
            service.frappe, "get_all"
        ) as get_all, patch.object(
            service, "_weekly_assigned_count", return_value=16
        ), patch.object(
            service, "_mandatory_unassigned", return_value=(recommendations, [])
        ), patch.object(
            service, "_hybrid_sampling_eligible", return_value=hybrid
        ), patch.object(
            service, "_week_start", return_value=datetime(2026, 10, 12)
        ), patch.object(
            service, "now_datetime", return_value=datetime(2026, 10, 14, 18, 0)
        ), patch.object(
            service, "add_days", return_value=datetime(2026, 10, 28, 18, 0)
        ), patch.object(
            service,
            "_advance_shared_recommendation_cadence",
            return_value={"status": "Not Configured", "advanced": False},
        ):
            result = service.run_shared_review_capacity()

        self.assertEqual(result["assigned"], 4)
        self.assertEqual(len(result["recommendation_qc"]), 2)
        self.assertEqual(len(result["hybrid_qc"]), 2)
        self.assertEqual(len(result["hybrid_not_selected"]), 1)
        selected_calls = [
            call.args[2]
            for call in database.set_value.call_args_list
            if call.args[0] == service.CANDIDATE_DOCTYPE
            and call.args[2].get("automation_status") == "QC Assigned"
        ]
        not_selected_calls = [
            call.args[2]
            for call in database.set_value.call_args_list
            if call.args[0] == service.CANDIDATE_DOCTYPE
            and call.args[2].get("automated_qc_review_status") == "Not Selected"
        ]
        self.assertEqual(len(selected_calls), 2)
        self.assertTrue(
            all(values["automated_qc_review_status"] == "Unreviewed" for values in selected_calls)
        )
        self.assertEqual(len(not_selected_calls), 1)
        get_all.assert_not_called()
        database.commit.assert_called_once_with()

    def test_hybrid_is_excluded_from_mandatory_candidate_backlog(self):
        settings = types.SimpleNamespace(automatic_tiered_canary="")
        ordinary = types.SimpleNamespace(
            name="splink", pair_key="s", automation_channel="Splink"
        )
        hybrid = types.SimpleNamespace(
            name="hybrid", pair_key="h", automation_channel="Hybrid Development"
        )
        with patch.object(
            service.frappe, "get_single", return_value=settings
        ), patch.object(
            service.frappe,
            "get_all",
            side_effect=[[], [ordinary, hybrid]],
        ):
            recommendations, candidates = service._mandatory_unassigned()

        self.assertEqual(recommendations, [])
        self.assertEqual([row.name for row in candidates], ["splink"])

    def test_shared_cadence_does_not_move_before_it_is_due(self):
        settings = types.SimpleNamespace(
            automatic_tiered_canary="canary-run",
            qc_assignment_interval_days=7,
        )
        next_at = datetime(2026, 10, 20, 10, 30, 0)
        run = types.SimpleNamespace(
            name="canary-run",
            qc_next_assignment_at=next_at,
            qc_assignment_cycle_count=5,
        )
        database = MagicMock()
        database.exists.return_value = True
        with patch.object(service.frappe, "db", database), patch.object(
            service.frappe, "get_doc", return_value=run
        ), patch.object(service, "_lock_rows"), patch.object(
            service, "_append_event"
        ) as append_event:
            result = service._advance_shared_recommendation_cadence(
                settings,
                now=datetime(2026, 10, 13, 10, 30, 0),
                recommendation_names=("recommendation-1",),
                already_used=2,
            )

        self.assertFalse(result["advanced"])
        self.assertEqual(result["status"], "Not Due")
        self.assertEqual(result["next_assignment_at"], next_at)
        database.set_value.assert_not_called()
        append_event.assert_not_called()

    def test_current_shared_qc_cadence_does_not_block_tiered_enablement(self):
        settings = types.SimpleNamespace(
            automatic_tiered_enabled=0,
            materialization_enabled=1,
            automation_paused=0,
            automatic_qc_assignment_enabled=1,
            automatic_tiered_canary="canary-run",
            automatic_tiered_policy="pilot-1.8",
            automatic_tiered_authorization_event=None,
            automatic_tiered_components_per_run=2,
        )
        canary = types.SimpleNamespace(
            status="Active",
            matching_policy="pilot-1.8",
            qc_last_assignment_at=datetime(2026, 10, 13, 10, 30, 0),
            qc_next_assignment_at=datetime(2026, 10, 20, 10, 30, 0),
            qc_overdue_count=0,
        )
        database = MagicMock()
        database.get_value.side_effect = ["Pilot", canary]
        database.count.return_value = 0
        with patch.object(tiered_automation.frappe, "db", database), patch.object(
            tiered_automation,
            "now_datetime",
            return_value=datetime(2026, 10, 13, 10, 31, 0),
        ):
            blockers = tiered_automation._configuration_blockers(
                settings, require_enabled=False
            )

        self.assertNotIn("qc_assignment_cadence_overdue", blockers)
        self.assertEqual(blockers, ["automation_authorization_event_missing"])

    def test_automatic_splink_selection_reports_complete_triple(self):
        settings = types.SimpleNamespace(
            maximum_splink_component_size=3,
            automatic_splink_components_per_run=10,
        )
        rows = [
            {"name": "AB", "left_record": "A", "right_record": "B"},
            {"name": "AC", "left_record": "A", "right_record": "C"},
            {"name": "BC", "left_record": "B", "right_record": "C"},
            {"name": "DE", "left_record": "D", "right_record": "E"},
            {"name": "EF", "left_record": "E", "right_record": "F"},
        ]
        queue = types.SimpleNamespace(name="ready-queue")
        with patch.object(
            service, "_active_queue_for_run", return_value=queue
        ), patch.object(service, "_scheduled_candidates", return_value=rows):
            selection = service._automatic_splink_selection(
                settings, types.SimpleNamespace(name="validation-run")
            )

        summary = selection["summary"]
        self.assertEqual(summary["maximum_component_size"], 3)
        self.assertEqual(summary["available_complete_size_counts"], {"3": 1})
        self.assertEqual(summary["selected_size_counts"], {"3": 1})
        self.assertEqual(summary["selected_component_count"], 1)
        self.assertEqual(summary["selected_candidate_count"], 3)
        self.assertEqual(summary["incomplete_component_count"], 1)

    def test_manual_splink_cycle_uses_requesting_manager(self):
        with patch.object(service, "_require_manager") as require_manager, patch.object(
            service.frappe,
            "session",
            types.SimpleNamespace(user="manager@example.com"),
        ), patch.object(
            service,
            "run_automatic_splink_cycle",
            return_value={"status": "Queued", "batch": "batch-1"},
        ) as run_cycle:
            result = service.run_automatic_splink_now()

        require_manager.assert_called_once_with()
        run_cycle.assert_called_once_with(requested_by="manager@example.com")
        self.assertEqual(result["batch"], "batch-1")

    def test_desk_exposes_splink_preview_and_manual_cycle(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Preview Automatic Splink Run", source)
        self.assertIn("Run One Automatic Splink Cycle Now", source)
        self.assertIn("preview_automatic_splink_cycle", source)
        self.assertIn("run_automatic_splink_now", source)


if __name__ == "__main__":
    unittest.main()
