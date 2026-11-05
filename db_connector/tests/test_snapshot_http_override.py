import inspect
import unittest

from db_connector import ccd_dashboard_snapshot as snapshot


class SnapshotHttpOverrideTests(unittest.TestCase):
    def test_finish_sync_override_remains_post_whitelisted(self):
        source = inspect.getsource(snapshot.finish_agent_sync_with_snapshot)

        self.assertTrue(
            source.startswith('@frappe.whitelist(methods=["POST"])'),
            "The finish_sync override must itself be whitelisted because Frappe "
            "authorizes the replacement method after resolving the hook override.",
        )


if __name__ == "__main__":
    unittest.main()
