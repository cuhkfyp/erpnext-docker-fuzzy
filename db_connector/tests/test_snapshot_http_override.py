import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from db_connector import api_agent_sync
from db_connector import ccd_dashboard_snapshot as snapshot


class SnapshotHttpOverrideTests(unittest.TestCase):
    def test_finish_sync_override_remains_post_whitelisted(self):
        source = inspect.getsource(snapshot.finish_agent_sync_with_snapshot)

        self.assertTrue(
            source.startswith('@frappe.whitelist(methods=["POST"])'),
            "The finish_sync override must itself be whitelisted because Frappe "
            "authorizes the replacement method after resolving the hook override.",
        )

    def test_agent_postprocess_defers_intermediate_snapshot_markers(self):
        flags = SimpleNamespace(
            ccd_dashboard_snapshot_defer_agent_postprocess=True,
            ccd_dashboard_snapshot_dirty_reasons=None,
        )
        with patch.object(snapshot.frappe, "flags", flags), patch.object(
            snapshot, "schema_ready"
        ) as schema_ready:
            snapshot.mark_dirty_after_commit(reason="membership-insert")

        schema_ready.assert_not_called()
        self.assertEqual(
            flags.ccd_dashboard_snapshot_dirty_reasons,
            {"membership-insert"},
        )

    def test_terminal_agent_postprocess_registers_one_snapshot_marker(self):
        source = inspect.getsource(api_agent_sync.run_postprocess_batch)

        self.assertIn(
            "frappe.flags.ccd_dashboard_snapshot_defer_agent_postprocess = True",
            source,
        )
        self.assertIn(
            "frappe.flags.ccd_dashboard_snapshot_defer_agent_postprocess = False",
            source,
        )
        self.assertEqual(
            source.count(
                'mark_dirty_after_commit(reason="agent-postprocessing-completed")'
            ),
            1,
        )

    def test_recovery_chain_nonce_is_propagated(self):
        enqueue_source = inspect.getsource(api_agent_sync._enqueue_postprocess)
        batch_source = inspect.getsource(api_agent_sync.run_postprocess_batch)

        self.assertIn('job_material = f"{run_id}:{cursor}"', enqueue_source)
        self.assertIn('job_material = f"{job_material}:{chain_nonce}"', enqueue_source)
        self.assertIn('chain_nonce=str(chain_nonce or "")', enqueue_source)
        self.assertIn('chain_nonce=str(chain_nonce or "")', batch_source)

    def test_snapshot_work_yields_during_agent_postprocessing(self):
        with patch.object(snapshot, "schema_ready", return_value=True), patch.object(
            snapshot, "_agent_postprocessing_active", return_value=True
        ):
            for operation in (
                snapshot.refresh_snapshot,
                snapshot.run_snapshot_maintenance,
                snapshot.run_snapshot_safety_check,
            ):
                self.assertEqual(
                    operation(),
                    {
                        "status": "Deferred",
                        "reason": "Agent post-processing active",
                    },
                )


if __name__ == "__main__":
    unittest.main()
