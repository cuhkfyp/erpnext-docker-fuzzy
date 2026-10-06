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

    def test_repeated_approve_all_preview_reuses_background_operation(self):
        database = MagicMock()
        database.sql.return_value = [("canary1",)]
        cache = MagicMock()
        cache.get_value.return_value = "b" * 32
        with patch.object(activation, "_require_manager"), patch.object(
            activation, "_run", return_value=SimpleNamespace(name="canary1")
        ), patch.object(activation.frappe, "db", database), patch.object(
            activation.frappe, "cache", cache
        ), patch.object(
            activation.frappe, "session", SimpleNamespace(user="manager@example.com")
        ), patch.object(
            activation,
            "_approve_all_preview_operation",
            return_value={"status": "Running", "requested_by": "manager@example.com"},
        ), patch.object(activation.frappe, "enqueue") as enqueue:
            result = activation.start_approve_all_preview("canary1")
        self.assertEqual(result["operation_token"], "b" * 32)
        self.assertTrue(result["already_running"])
        enqueue.assert_not_called()

    def test_approve_all_preview_worker_is_zero_write_and_publishes_result(self):
        payload = {"requested_by": "manager@example.com", "status": "Queued"}
        run = SimpleNamespace(name="canary1")
        preview = {
            "run": "canary1",
            "zero_write": True,
            "selected_component_count": 2,
            "unsafe_component_count": 0,
            "components": [],
        }
        selected = [("component-a", []), ("component-b", [])]
        with patch.object(
            activation.frappe, "get_roles", return_value=["System Manager"]
        ), patch.object(activation.frappe, "set_user"), patch.object(
            activation, "_approve_all_preview_operation", return_value=payload
        ), patch.object(
            activation, "_set_approve_all_preview_operation"
        ) as set_status, patch.object(
            activation, "_run", return_value=run
        ), patch.object(
            activation, "_selected_components", return_value=selected
        ), patch.object(
            activation, "_preview_components", return_value=preview
        ) as preview_components, patch.object(
            activation.frappe, "get_doc"
        ) as get_doc:
            result = activation.run_approve_all_preview(
                "b" * 32, "canary1", "manager@example.com"
            )
        self.assertTrue(result["zero_write"])
        self.assertEqual(set_status.call_args.args[1]["status"], "Completed")
        self.assertEqual(set_status.call_args.args[1]["result"], preview)
        self.assertFalse(preview_components.call_args.kwargs["include_safe_components"])
        get_doc.assert_not_called()

    def test_component_preview_uses_bounded_pages_and_reports_progress(self):
        run = SimpleNamespace(name="canary1", policy_snapshot_json="{}")
        selected = [
            (f"component-{index}", [SimpleNamespace(name=f"recommendation-{index}")])
            for index in range(3)
        ]

        def page(_run, values, *, policy):
            return [
                {
                    "component_fingerprint": key,
                    "recommendation_names": [rows[0].name],
                    "recommendation_count": 1,
                    "record_count": 2,
                    "safe": key != "component-1",
                    "conflicts": [] if key != "component-1" else ["unsafe"],
                }
                for key, rows in values
            ]

        progress = MagicMock()
        with patch.object(activation, "PREVIEW_COMPONENT_PAGE_SIZE", 2), patch.object(
            activation, "_matching_policy", return_value=MagicMock()
        ), patch.object(
            activation, "_preview_component_page", side_effect=page
        ) as preview_page:
            result = activation._preview_components(
                run, selected, progress_callback=progress
            )
        self.assertEqual(preview_page.call_count, 2)
        self.assertEqual(result["safe_component_count"], 2)
        self.assertEqual(result["unsafe_component_count"], 1)
        self.assertEqual(result["planned_membership_count"], 4)
        self.assertEqual(progress.call_args_list[-1].args, (3, 3))

    def test_component_lookup_is_bounded_to_frozen_batch_keys(self):
        with patch.object(activation.frappe, "get_all", return_value=[]) as get_all:
            result = activation._component_rows(
                "canary1", component_keys=["component-b", "component-a", "component-a"]
            )
        self.assertEqual(result, {})
        self.assertEqual(
            get_all.call_args.kwargs["filters"],
            {
                "canary_run": "canary1",
                "status": "Proposed",
                "cluster_fingerprint": [
                    "in", ("component-a", "component-b")
                ],
            },
        )

    def test_empty_frozen_component_lookup_skips_database(self):
        with patch.object(activation.frappe, "get_all") as get_all:
            result = activation._component_rows("canary1", component_keys=[])
        self.assertEqual(result, {})
        get_all.assert_not_called()

    def test_already_materialized_component_is_not_selectable(self):
        row = SimpleNamespace(cluster_fingerprint="component-a", rollout_state="Available")
        with patch.object(
            activation, "_component_rows", return_value={"component-a": [row]}
        ), patch.object(
            activation, "_materialized_component_matches",
            return_value={"component-a": {"identity_group": "group1"}},
        ) as materialized_matches:
            selected = activation._selected_components("canary1")
        self.assertEqual(selected, [])
        materialized_matches.assert_called_once_with("canary1")

    def test_automatic_key_page_is_indexed_and_keyset_bounded(self):
        database = MagicMock()
        database.sql.return_value = [
            SimpleNamespace(cluster_fingerprint="component-c"),
            SimpleNamespace(cluster_fingerprint="component-d"),
        ]
        with patch.object(activation.frappe, "db", database):
            result = activation._automatic_component_key_page(
                "canary1", after_key="component-b", page_size=200
            )
        self.assertEqual(result, ("component-c", "component-d"))
        query = database.sql.call_args.args[0]
        self.assertIn("canary_run = %s", query)
        self.assertIn("cluster_fingerprint > %s", query)
        self.assertIn("LIMIT 200", query)
        self.assertEqual(database.sql.call_args.args[1], ("canary1", "component-b"))

    def test_automatic_selection_stops_after_first_safe_limit(self):
        run = SimpleNamespace(name="canary1")
        rows = {
            key: [SimpleNamespace(name=f"recommendation-{key}")]
            for key in ("component-a", "component-b", "component-c")
        }

        def preview(_run, selected):
            components = [
                {
                    "component_fingerprint": key,
                    "safe": key != "component-a",
                    "conflicts": ["unsafe"] if key == "component-a" else [],
                }
                for key, _component_rows in selected
            ]
            safe_count = sum(int(item["safe"]) for item in components)
            return {
                "run": "canary1",
                "zero_write": True,
                "selected_component_count": len(selected),
                "selected_recommendation_count": len(selected),
                "safe_component_count": safe_count,
                "unsafe_component_count": len(selected) - safe_count,
                "stale_component_count": 0,
                "planned_identity_group_count": safe_count,
                "planned_membership_count": safe_count * 2,
                "conflict_counts": {},
                "components": components,
            }

        with patch.object(activation, "_run", return_value=run), patch.object(
            activation,
            "_automatic_component_pages",
            return_value=iter(
                [
                    (
                        ("component-a", rows["component-a"]),
                        ("component-b", rows["component-b"]),
                        ("component-c", rows["component-c"]),
                    ),
                    (("component-never-read", []),),
                ]
            ),
        ), patch.object(activation, "_preview_components", side_effect=preview), patch.object(
            activation, "_selected_components"
        ) as unbounded_selector:
            result = activation.preview_automatic_component_selection("canary1", 2)
        self.assertEqual(
            result["component_keys"], ["component-b", "component-c"]
        )
        self.assertEqual(result["skipped_unsafe_component_count"], 1)
        unbounded_selector.assert_not_called()

    def test_approved_delta_updates_canary_counters_without_run_scan(self):
        database = MagicMock()
        database.get_value.return_value = {
            "proposed_count": 67_134,
            "exception_count": 7_362,
            "active_count": 97,
            "reversed_count": 0,
            "superseded_count": 53,
        }
        with patch.object(activation.frappe, "db", database), patch.object(
            activation.frappe, "get_all"
        ) as get_all:
            result = activation._apply_approved_recommendation_delta("canary1", 5)
        update_sql, parameters = database.sql.call_args.args[:2]
        self.assertIn("proposed_count=GREATEST", update_sql)
        self.assertIn("active_count=COALESCE(active_count,0)+%s", update_sql)
        self.assertEqual(parameters, (5, 5, "canary1"))
        self.assertEqual(result["proposed_count"], 67_134)
        self.assertEqual(result["active_count"], 97)
        database.commit.assert_not_called()
        get_all.assert_not_called()

    def test_approve_all_apply_uses_one_generic_delta_and_async_recount(self):
        first_item = SimpleNamespace(
            status="Planned",
            component_fingerprint="component-a",
            recommendation_names_json='["recommendation-a1", "recommendation-a2"]',
            db_set=MagicMock(),
        )
        second_item = SimpleNamespace(
            status="Planned",
            component_fingerprint="component-b",
            recommendation_names_json=(
                '["recommendation-b1", "recommendation-b2", "recommendation-b3"]'
            ),
            db_set=MagicMock(),
        )
        batch = SimpleNamespace(
            name="batch-all",
            is_automatic=0,
            status="Approved",
            canary_run="canary1",
            policy_snapshot_sha256="snapshot-sha",
            items=[first_item, second_item],
            created_group_count=0,
            created_membership_count=0,
            selection_method="Approve All Eligible",
            is_demonstration=0,
            db_set=MagicMock(),
        )
        run = SimpleNamespace(
            name="canary1",
            policy_snapshot_sha256="snapshot-sha",
            policy_snapshot_json="{}",
            matching_policy="pilot-1",
        )
        rows = {
            "component-a": [
                SimpleNamespace(name="recommendation-a1", rollout_state="Available"),
                SimpleNamespace(name="recommendation-a2", rollout_state="Available"),
            ],
            "component-b": [
                SimpleNamespace(name="recommendation-b1", rollout_state="Available"),
                SimpleNamespace(name="recommendation-b2", rollout_state="Available"),
                SimpleNamespace(name="recommendation-b3", rollout_state="Available"),
            ],
        }
        recommendations = {
            row.name: SimpleNamespace(
                name=row.name, status="Proposed", canary_run="canary1"
            )
            for component_rows in rows.values()
            for row in component_rows
        }

        def get_doc(doctype, name):
            if doctype == activation.BATCH_DOCTYPE:
                return batch
            return recommendations[name]

        database = MagicMock()
        database.count.side_effect = [202, 404]
        with patch.object(activation, "_require_manager"), patch.object(
            activation, "_run", return_value=run
        ), patch.object(activation, "_component_rows", return_value=rows), patch.object(
            activation,
            "_component_context",
            return_value={
                "record_ids": ["record-a", "record-b"],
                "expected_modified": {},
                "expected_fingerprints": {},
            },
        ), patch.object(activation, "_reasons", return_value=[]), patch.object(
            activation,
            "materialize_identity",
            side_effect=[
                {
                    "status": "Applied",
                    "identity_decision": "decision-a",
                    "identity_groups": ["group-a"],
                    "created_groups": 1,
                    "created_memberships": 2,
                },
                {
                    "status": "Applied",
                    "identity_decision": "decision-b",
                    "identity_groups": ["group-b"],
                    "created_groups": 1,
                    "created_memberships": 2,
                },
            ],
        ), patch.object(activation, "_change_recommendation_status"), patch.object(
            activation,
            "_apply_approved_recommendation_delta",
            return_value={"active_count": 97},
        ) as apply_delta, patch.object(
            activation, "_refresh_run_counts"
        ) as full_recount, patch.object(
            activation,
            "_enqueue_activation_run_count_reconciliation",
            return_value="reconcile-job",
        ) as enqueue_recount, patch.object(
            activation.frappe, "get_doc", side_effect=get_doc
        ), patch.object(
            activation.frappe, "db", database
        ), patch.object(
            activation.frappe,
            "session",
            SimpleNamespace(user="manager@example.com"),
        ), patch.object(
            activation.frappe.utils,
            "now_datetime",
            return_value="2026-09-28 12:00:00",
        ), patch(
            "db_connector.ccd_dashboard_snapshot.mark_dirty_after_commit"
        ):
            result = activation._apply_activation_batch("batch-all")

        apply_delta.assert_called_once_with("canary1", 5)
        full_recount.assert_not_called()
        enqueue_recount.assert_called_once_with("canary1", "batch-all")
        database.commit.assert_called_once()
        self.assertEqual(result["approved_recommendations"], 97)
        self.assertEqual(result["run_count_reconciliation_job"], "reconcile-job")

    def test_reconciliation_is_queued_after_commit_on_long_worker(self):
        with patch.object(activation.frappe, "enqueue") as enqueue:
            job_id = activation._enqueue_activation_run_count_reconciliation(
                "canary1", "batch-all"
            )
        self.assertEqual(job_id, "ccd-canary-count-reconcile-batch-all")
        self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
        self.assertFalse(enqueue.call_args.kwargs["enqueue_after_commit"])
        self.assertEqual(enqueue.call_args.kwargs["run_name"], "canary1")
        self.assertEqual(enqueue.call_args.kwargs["batch_name"], "batch-all")


if __name__ == "__main__":
    unittest.main()
