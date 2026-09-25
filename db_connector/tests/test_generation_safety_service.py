"""Focused tests for generation writes and destructive-operation retries."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from db_connector import api_fuzzy_canary as canary
from db_connector import api_identity_retirement as retirement
from db_connector.fuzzy_matching.canary import CanaryEdge


class CanaryGenerationSafetyTests(unittest.TestCase):
    def test_second_start_returns_the_running_canary_without_enqueuing(self):
        database = MagicMock()
        database.sql.side_effect = [
            [("CCD Match Canary Run",)],
            [SimpleNamespace(name="existing", matching_policy="pilot-1.8", status="Profiling")],
        ]
        with patch.object(canary.frappe, "db", database), patch.object(
            canary, "_canary_prerequisites"
        ) as prerequisites, patch.object(canary, "_reconcile_terminated_canary", return_value=False), patch.object(
            canary.frappe, "enqueue"
        ) as enqueue:
            result = canary._create_canary_run("pilot-1.8")
        self.assertEqual(result["run"], "existing")
        self.assertTrue(result["already_running"])
        prerequisites.assert_not_called()
        enqueue.assert_not_called()
        self.assertIn("FOR UPDATE", database.sql.call_args_list[1].args[0])

    def test_bulk_write_links_one_recommendation_to_one_event(self):
        database = MagicMock()
        run = SimpleNamespace(
            name="run1", matching_policy="pilot-1.8", policy_version="pilot-1.8"
        )
        rows = {
            "A": {"source": "S1", "source_modified": "2026-09-22 00:00:00"},
            "B": {"source": "S2", "source_modified": "2026-09-22 00:00:00"},
        }
        decisions = {
            ("A", "B"): SimpleNamespace(
                status="Proposed", reasons=(), cluster_fingerprint="cluster1",
                cluster_size=2,
            )
        }
        with patch.object(canary.frappe, "db", database), patch.object(
            canary.frappe, "session", SimpleNamespace(user="Administrator")
        ), patch.object(canary, "identity_fingerprint", return_value="fingerprint"), patch.object(
            canary.frappe.utils, "now_datetime", return_value="2026-09-22 00:00:00"
        ):
            counts = canary._write_generated_recommendations(
                run, [CanaryEdge("A", "B", "S1", "S2", "S1::S2")],
                decisions, rows, SimpleNamespace(),
            )
        self.assertEqual(counts[1]["Proposed"], 1)
        self.assertEqual(database.bulk_insert.call_count, 2)
        rec_call, event_call = database.bulk_insert.call_args_list
        rec_fields = rec_call.args[1]
        rec = dict(zip(rec_fields, rec_call.args[2][0]))
        event_fields = event_call.args[1]
        event = dict(zip(event_fields, event_call.args[2][0]))
        self.assertEqual(rec["name"], event["recommendation"])
        self.assertEqual(rec["recommendation_key"], canary._recommendation_key("run1", "A", "B"))
        self.assertEqual(event["event_type"], "Created")
        self.assertEqual(rec["rollout_state"], "Available")
        database.commit.assert_not_called()

    def test_failure_reconnects_before_marking_run_failed(self):
        database = MagicMock()
        database.rollback.side_effect = RuntimeError("connection closed")
        with patch.object(canary.frappe, "db", database):
            canary._record_failed_canary("run1", TimeoutError())
        database.connect.assert_called_once()
        self.assertEqual(database.set_value.call_args.args[2]["status"], "Failed")
        database.commit.assert_called_once()

    def test_failed_queue_job_is_released_only_when_no_rows_were_committed(self):
        database = MagicMock()
        database.count.return_value = 0
        with patch.object(canary.frappe, "db", database), patch(
            "frappe.utils.background_jobs.get_job_status", return_value="failed"
        ):
            released = canary._reconcile_terminated_canary(
                SimpleNamespace(name="run1")
            )
        self.assertTrue(released)
        self.assertEqual(database.set_value.call_args.args[2]["status"], "Failed")

    def test_live_queue_job_is_never_released(self):
        database = MagicMock()
        with patch.object(canary.frappe, "db", database), patch(
            "frappe.utils.background_jobs.get_job_status", return_value="started"
        ):
            released = canary._reconcile_terminated_canary(
                SimpleNamespace(name="run1")
            )
        self.assertFalse(released)
        database.count.assert_not_called()
        database.set_value.assert_not_called()

    def test_exact_active_partition_is_detected_as_already_materialized(self):
        components = {
            "component1": [
                SimpleNamespace(
                    name="rec1",
                    canary_run="run1",
                    cluster_fingerprint="component1",
                    left_record="A",
                    right_record="B",
                    left_identity_fingerprint="fp-a",
                    right_identity_fingerprint="fp-b",
                )
            ]
        }

        def get_all(doctype, **kwargs):
            filters = kwargs.get("filters", {})
            if doctype == "CCD Identity Group":
                return [
                    SimpleNamespace(
                        name="group1", status="Active", originating_decision="decision1"
                    )
                ]
            if "ccd_master" in filters:
                return [
                    SimpleNamespace(
                        name="member-a", ccd_master="A", identity_group="group1",
                        identity_fingerprint="fp-a",
                    ),
                    SimpleNamespace(
                        name="member-b", ccd_master="B", identity_group="group1",
                        identity_fingerprint="fp-b",
                    ),
                ]
            if "identity_group" in filters:
                return [
                    SimpleNamespace(ccd_master="A", identity_group="group1"),
                    SimpleNamespace(ccd_master="B", identity_group="group1"),
                ]
            return []

        with patch.object(canary.frappe, "get_all", side_effect=get_all):
            matches = canary._materialized_component_matches("run1", components)
        self.assertEqual(matches["component1"]["identity_group"], "group1")
        self.assertEqual(matches["component1"]["identity_decision"], "decision1")

    def test_partial_existing_group_is_not_reconciled(self):
        components = {
            "component1": [
                SimpleNamespace(
                    name="rec1", canary_run="run1", cluster_fingerprint="component1",
                    left_record="A", right_record="B",
                    left_identity_fingerprint="fp-a", right_identity_fingerprint="fp-b",
                )
            ]
        }

        def get_all(doctype, **kwargs):
            filters = kwargs.get("filters", {})
            if doctype == "CCD Identity Group":
                return [
                    SimpleNamespace(
                        name="group1", status="Active", originating_decision="decision1"
                    )
                ]
            if "ccd_master" in filters:
                return [
                    SimpleNamespace(
                        name="member-a", ccd_master="A", identity_group="group1",
                        identity_fingerprint="fp-a",
                    ),
                    SimpleNamespace(
                        name="member-b", ccd_master="B", identity_group="group1",
                        identity_fingerprint="fp-b",
                    ),
                ]
            if "identity_group" in filters:
                return [
                    SimpleNamespace(ccd_master="A", identity_group="group1"),
                    SimpleNamespace(ccd_master="B", identity_group="group1"),
                    SimpleNamespace(ccd_master="C", identity_group="group1"),
                ]
            return []

        with patch.object(canary.frappe, "get_all", side_effect=get_all):
            matches = canary._materialized_component_matches("run1", components)
        self.assertEqual(matches, {})

    def test_reconciliation_closes_redundant_work_without_new_decision(self):
        recommendation = SimpleNamespace(
            name="rec1", canary_run="run1", status="Proposed"
        )
        components = {"component1": [recommendation]}
        database = MagicMock()
        with patch.object(
            canary, "_materialized_component_matches",
            return_value={
                "component1": {
                    "identity_group": "group1",
                    "identity_decision": "decision1",
                    "record_count": 2,
                    "recommendation_count": 1,
                }
            },
        ), patch.object(canary.frappe, "db", database), patch.object(
            canary.frappe, "session", SimpleNamespace(user="Administrator")
        ), patch.object(
            canary.frappe.utils, "now_datetime", return_value="2026-09-25 00:00:00"
        ), patch.object(canary.frappe, "get_doc") as get_doc:
            result = canary.reconcile_materialized_recommendations("run1", components)
        self.assertEqual(result["component_count"], 1)
        self.assertEqual(result["recommendation_count"], 1)
        database.bulk_update.assert_called_once()
        values = database.bulk_update.call_args.args[1][0]
        self.assertEqual(values["status"], "Superseded")
        self.assertEqual(values["rollout_state"], "Superseded")
        self.assertEqual(values["identity_group"], "group1")
        self.assertEqual(values["identity_decision"], "decision1")
        database.bulk_insert.assert_called_once()
        event_fields = database.bulk_insert.call_args.args[1]
        event_values = database.bulk_insert.call_args.args[2][0]
        event = dict(zip(event_fields, event_values))
        self.assertEqual(event["recommendation"], "rec1")
        self.assertEqual(event["event_type"], "Superseded")
        self.assertEqual(event["reason"], "already_materialized_current_partition")
        get_doc.assert_not_called()


class RegistrationRetirementRetryTests(unittest.TestCase):
    def test_apply_active_key_is_shared_across_managers(self):
        self.assertEqual(
            retirement._registration_operation_active_key("Apply", "R1", "manager1"),
            retirement._registration_operation_active_key("Apply", "R1", "manager2"),
        )
        self.assertNotEqual(
            retirement._registration_operation_active_key("Preview", "R1", "manager1"),
            retirement._registration_operation_active_key("Preview", "R1", "manager2"),
        )

    def test_cancelled_registration_requires_matching_applied_retirement(self):
        fingerprint = "a" * 64
        database = MagicMock()
        database.get_value.return_value = SimpleNamespace(
            name="retirement1", result_json=json.dumps({"source_name": "S1"})
        )
        registration = SimpleNamespace(name="R1", docstatus=2)
        with patch.object(retirement.frappe, "db", database), patch.object(
            retirement, "registration_source_key", return_value="S1"
        ):
            result = retirement._applied_registration_cancellation(registration, fingerprint)
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["retirement_run"], "retirement1")
        self.assertEqual(result["registration_status"], "Cancelled")

    def test_second_confirmed_click_reuses_operation_under_registration_lock(self):
        database = MagicMock()
        database.sql.return_value = [(1,)]
        existing = {
            "operation_token": "token1", "status": "Running", "already_running": True
        }
        with patch.object(retirement.frappe, "db", database), patch.object(
            retirement, "_existing_registration_operation", return_value=existing
        ), patch.object(retirement.frappe, "enqueue") as enqueue:
            result = retirement._queue_registration_operation(
                operation="Apply", registration_name="R1", requested_by="manager1",
                method="run_method", timeout=7200,
                kwargs={"confirm_scope_fingerprint": "a" * 64},
            )
        self.assertEqual(result, existing)
        self.assertIn("FOR UPDATE", database.sql.call_args.args[0])
        enqueue.assert_not_called()

    def test_cancelled_registration_with_wrong_source_is_not_retryable(self):
        database = MagicMock()
        database.get_value.return_value = SimpleNamespace(
            name="retirement1", result_json=json.dumps({"source_name": "OTHER"})
        )
        with patch.object(retirement.frappe, "db", database), patch.object(
            retirement, "registration_source_key", return_value="S1"
        ):
            result = retirement._applied_registration_cancellation(
                SimpleNamespace(name="R1", docstatus=2), "a" * 64
            )
        self.assertIsNone(result)

    def test_empty_source_retirement_is_recorded_for_future_retries(self):
        fingerprint = "b" * 64
        database = MagicMock()
        database.get_value.return_value = None
        database.sql.side_effect = [[], []]
        run = SimpleNamespace(name="retirement-empty")
        run.insert = MagicMock(return_value=run)
        with patch.object(retirement.frappe, "db", database), patch.object(
            retirement.frappe, "session", SimpleNamespace(user="Administrator")
        ), patch.object(
            retirement.frappe.utils, "now_datetime", return_value="2026-09-22 00:00:00"
        ), patch.object(
            retirement.frappe, "get_doc", return_value=run
        ), patch.object(
            retirement, "_control_state", return_value={
                "materialization_enabled": False,
                "automatic_tiered_enabled": False,
                "automatic_qc_assignment_enabled": False,
            }
        ), patch.object(
            retirement, "resolve_source_key", return_value="S1"
        ), patch.object(
            retirement, "_source_record_ids", return_value=()
        ), patch.object(
            retirement, "_source_state", return_value=(None, {"scope_fingerprint": fingerprint})
        ), patch.object(
            retirement, "_lock_names"
        ), patch.object(
            retirement, "_update_registration_operation_progress"
        ):
            result = retirement._apply_source_retirement(
                "S1", None, fingerprint, "test cancellation", commit=False
            )
        self.assertEqual(result["retirement_run"], "retirement-empty")
        self.assertEqual(result["deleted_ccd_masters"], 0)
        self.assertEqual(database.set_value.call_args.args[2]["status"], "Applied")
        database.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
