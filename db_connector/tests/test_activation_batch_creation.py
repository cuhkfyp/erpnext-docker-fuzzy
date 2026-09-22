"""The Desk batch-creation request must outlive an HTTP response timeout."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from db_connector import api_identity_activation as activation


class ActivationBatchCreationOperationTests(unittest.TestCase):
    def setUp(self):
        flags = patch.object(
            activation.frappe.local,
            "flags",
            SimpleNamespace(in_test=True),
            create=True,
        )
        flags.start()
        self.addCleanup(flags.stop)
        clock = patch.object(
            activation.frappe.utils, "now_datetime", return_value="2026-09-22 00:00:00"
        )
        clock.start()
        self.addCleanup(clock.stop)

    def test_repeated_click_reuses_running_operation_without_second_job(self):
        database = MagicMock()
        database.sql.return_value = [("canary1",)]
        cache = MagicMock()
        cache.get_value.return_value = "a" * 32
        with patch.object(activation, "_require_manager"), patch.object(
            activation, "_run", return_value=SimpleNamespace(name="canary1")
        ), patch.object(activation.frappe, "db", database), patch.object(
            activation.frappe, "cache", cache
        ), patch.object(
            activation.frappe, "session", SimpleNamespace(user="manager@example.com")
        ), patch.object(
            activation, "_creation_operation",
            return_value={"status": "Running", "requested_by": "manager@example.com"},
        ), patch.object(activation.frappe, "enqueue") as enqueue:
            result = activation.start_activation_batch_creation(
                "canary1", component_limit=41, is_pilot_wave=1
            )
        self.assertEqual(result["operation_token"], "a" * 32)
        self.assertTrue(result["already_running"])
        self.assertIn("FOR UPDATE", database.sql.call_args.args[0])
        enqueue.assert_not_called()

    def test_new_request_enqueues_worker_and_returns_token(self):
        database = MagicMock()
        database.sql.return_value = [("canary1",)]
        cache = MagicMock()
        cache.get_value.return_value = None
        with patch.object(activation, "_require_manager"), patch.object(
            activation, "_run", return_value=SimpleNamespace(name="canary1")
        ), patch.object(activation.frappe, "db", database), patch.object(
            activation.frappe, "cache", cache
        ), patch.object(
            activation.frappe, "session", SimpleNamespace(user="manager@example.com")
        ), patch.object(activation.frappe, "enqueue") as enqueue:
            result = activation.start_activation_batch_creation(
                "canary1", component_limit=41, is_pilot_wave=1
            )
        self.assertEqual(result["status"], "Queued")
        self.assertEqual(len(result["operation_token"]), 32)
        self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
        self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
        database.commit.assert_called_once()

    def test_enqueue_failure_releases_the_active_request(self):
        database = MagicMock()
        database.sql.return_value = [("canary1",)]
        cache = MagicMock()
        cache.get_value.return_value = None
        with patch.object(activation, "_require_manager"), patch.object(
            activation, "_run", return_value=SimpleNamespace(name="canary1")
        ), patch.object(activation.frappe, "db", database), patch.object(
            activation.frappe, "cache", cache
        ), patch.object(
            activation.frappe, "session", SimpleNamespace(user="manager@example.com")
        ), patch.object(
            activation.frappe, "enqueue", side_effect=RuntimeError("queue unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
                activation.start_activation_batch_creation("canary1", component_limit=41)
        self.assertEqual(cache.delete_value.call_count, 2)
        database.commit.assert_not_called()

    def test_worker_reports_committed_batch_without_applying_it(self):
        payload = {"requested_by": "manager@example.com", "status": "Queued"}
        with patch.object(
            activation.frappe, "get_roles", return_value=["System Manager"]
        ), patch.object(activation.frappe, "set_user"), patch.object(
            activation, "_creation_operation", return_value=payload
        ), patch.object(activation, "_set_creation_operation") as set_status, patch.object(
            activation, "_create_activation_batch",
            return_value={"batch": "batch1", "status": "Reviewed"},
        ), patch.object(activation, "materialize_identity") as materialize:
            result = activation.run_activation_batch_creation(
                "a" * 32, "canary1", "Explicit Wave", 41, 1, 0,
                "manager@example.com",
            )
        self.assertEqual(result["batch"], "batch1")
        self.assertEqual(set_status.call_args.args[1]["status"], "Completed")
        self.assertEqual(set_status.call_args.args[1]["batch"], "batch1")
        materialize.assert_not_called()

    def test_status_cannot_be_read_by_another_manager(self):
        with patch.object(activation, "_require_manager"), patch.object(
            activation, "_creation_operation",
            return_value={"requested_by": "manager1", "status": "Completed", "batch": "batch1"},
        ), patch.object(
            activation.frappe, "session", SimpleNamespace(user="manager2")
        ), patch.object(activation.frappe, "throw", side_effect=RuntimeError("denied")):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                activation.get_activation_batch_creation("a" * 32)

    def test_stopped_worker_is_unknown_not_a_false_failure(self):
        payload = {"requested_by": "manager1", "status": "Running"}
        with patch.object(activation, "_require_manager"), patch.object(
            activation, "_creation_operation", return_value=payload
        ), patch.object(
            activation.frappe, "session", SimpleNamespace(user="manager1")
        ), patch(
            "frappe.utils.background_jobs.get_job_status", return_value="failed"
        ), patch.object(activation, "_set_creation_operation"):
            result = activation.get_activation_batch_creation("a" * 32)
        self.assertEqual(result["status"], "Unknown")
        self.assertIn("Check existing batches", result["error"])


if __name__ == "__main__":
    unittest.main()
