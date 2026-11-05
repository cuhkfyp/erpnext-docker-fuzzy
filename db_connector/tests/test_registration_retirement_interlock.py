import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from db_connector import api_agent_sync as agent_sync
from db_connector import api_identity_retirement as retirement


class FakeDoc:
    def __init__(self, **values):
        object.__setattr__(self, "values", dict(values))

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def __getattr__(self, key):
        try:
            return self.values[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


class RegistrationRetirementInterlockTests(unittest.TestCase):
    def test_retirement_uses_the_actual_agent_source_override(self):
        registration = FakeDoc(
            ccd_stable_source_key="DISPLAY_SOURCE",
            agent_sync_source_id="MASTER_SOURCE",
        )
        self.assertEqual(
            retirement.registration_retirement_source_key(registration),
            "MASTER_SOURCE",
        )

    def test_amended_revision_does_not_inherit_runtime_success(self):
        fields = [
            SimpleNamespace(fieldname=fieldname)
            for fieldname in {
                *agent_sync.AMENDED_OPERATIONAL_DEFAULTS,
                "log_info",
            }
        ]
        registration = FakeDoc(
            amended_from="old-revision",
            agent_sync_state="Succeeded",
            agent_sync_run_id="old-run",
            agent_sync_heartbeat_at="2026-11-03 16:40:37",
            agent_retirement_hold=1,
            log_info=[{"message": "old log"}],
        )
        with patch.object(
            agent_sync.frappe, "get_meta", return_value=SimpleNamespace(fields=fields)
        ):
            agent_sync.reset_amended_registration_operational_state(registration)
        self.assertEqual(registration.get("agent_sync_state"), "Idle")
        self.assertIsNone(registration.get("agent_sync_run_id"))
        self.assertIsNone(registration.get("agent_sync_heartbeat_at"))
        self.assertEqual(registration.get("agent_retirement_hold"), 0)
        self.assertEqual(
            registration.get("agent_baseline_adoption_state"), "Not Requested"
        )
        self.assertEqual(registration.get("agent_baseline_adoption_progress"), 0)
        self.assertEqual(registration.get("log_info"), [])

    def test_existing_baseline_confirmation_compares_every_required_field(self):
        field_types = {
            "ccd_reg_source": "Data",
            "ccd_source_key": "Data",
            "chi_firstname": "Data",
            "birthday": "Date",
        }
        meta = SimpleNamespace(
            fields=[
                SimpleNamespace(fieldname=name, fieldtype=fieldtype)
                for name, fieldtype in field_types.items()
            ],
            get_field=lambda name: SimpleNamespace(
                fieldname=name, fieldtype=field_types[name]
            ),
        )
        incoming = {
            "ccd_reg_source": "SOURCE",
            "ccd_source_key": "1",
            "chi_firstname": "大文",
            "birthday": "1961-10-06",
        }
        stored = FakeDoc(name="HKSR1", **incoming)
        with patch.object(agent_sync.frappe, "get_meta", return_value=meta), patch.object(
            agent_sync.frappe, "get_all", return_value=[stored]
        ):
            keys = agent_sync._confirm_existing_baseline_rows(
                [incoming],
                [incoming],
                "SOURCE",
                ["chi_firstname", "birthday"],
            )
        self.assertEqual(keys, ["1"])

        missing_field = dict(incoming)
        missing_field.pop("birthday")
        with patch.object(agent_sync.frappe, "get_meta", return_value=meta), patch.object(
            agent_sync.frappe, "throw", side_effect=RuntimeError("blocked")
        ):
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                agent_sync._confirm_existing_baseline_rows(
                    [missing_field],
                    [missing_field],
                    "SOURCE",
                    ["chi_firstname", "birthday"],
                )

    def test_failed_baseline_batch_never_falls_through_to_master_insert(self):
        database = MagicMock()
        doc = FakeDoc(name="R1", agent_sync_run_id="run-1")
        adoption = {
            "authorization_id": "auth-1",
            "snapshot": {
                "master_count": 2,
                "mapped_fields": ["chi_firstname"],
            },
        }
        rows = [
            {
                "ccd_reg_source": "SOURCE",
                "ccd_source_key": "1",
                "chi_firstname": "大文",
            }
        ]
        with patch.object(agent_sync, "_require_writer"), patch.object(
            agent_sync, "_registration", return_value=doc
        ), patch.object(
            agent_sync, "_validate_source", return_value="SOURCE"
        ), patch.object(
            agent_sync,
            "_config",
            return_value={"fast_insert_enabled": True, "batch_size": 500},
        ), patch.object(agent_sync, "_require_lease"), patch.object(
            agent_sync, "_validated_rows", return_value=rows
        ), patch.object(
            agent_sync, "_active_baseline_adoption", return_value=adoption
        ), patch.object(
            agent_sync,
            "_confirm_existing_baseline_rows",
            side_effect=RuntimeError("1:chi_firstname"),
        ), patch.object(
            agent_sync, "_baseline_progress_count", return_value=0
        ), patch.object(agent_sync, "_bulk_insert_rows") as bulk_insert, patch.object(
            agent_sync.frappe, "db", database
        ):
            result = inspect.unwrap(agent_sync.fast_insert_master_batch)(
                "R1", "SOURCE", "lease", "run-1", rows
            )
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["accepted"], 0)
        self.assertTrue(result["errors"])
        self.assertTrue(adoption["failed"])
        bulk_insert.assert_not_called()
        database.set_value.assert_called_once()

    def test_completed_baseline_releases_lease_without_postprocessing(self):
        database = MagicMock()
        cache = MagicMock()
        cache.eval.return_value = 1
        doc = FakeDoc(name="R1", agent_sync_run_id="run-1")
        adoption = {
            "authorization_id": "auth-1",
            "scope_fingerprint": "scope",
        }
        snapshot = {"master_count": 2, "mapping_count": 20}
        with patch.object(agent_sync, "_require_writer"), patch.object(
            agent_sync, "_registration", return_value=doc
        ), patch.object(
            agent_sync, "_validate_source", return_value="SOURCE"
        ), patch.object(agent_sync, "_require_lease"), patch.object(
            agent_sync, "_active_baseline_adoption", return_value=adoption
        ), patch.object(
            agent_sync, "_validated_baseline_population", return_value=snapshot
        ), patch.object(
            agent_sync, "_baseline_scope_fingerprint", return_value="scope"
        ), patch.object(
            agent_sync, "_baseline_progress_count", return_value=2
        ), patch.object(
            agent_sync, "_lease_keys", return_value=(b"source", b"slots")
        ), patch.object(agent_sync.frappe, "cache", cache), patch.object(
            agent_sync.frappe, "db", database
        ), patch.object(agent_sync, "_enqueue_postprocess") as enqueue:
            result = inspect.unwrap(agent_sync.finish_sync)(
                "R1", "SOURCE", "lease", "run-1", 1, [], []
            )
        self.assertEqual(result["state"], "Succeeded")
        self.assertTrue(result["baseline_adoption"])
        self.assertEqual(result["confirmed_existing"], 2)
        enqueue.assert_not_called()
        database.set_value.assert_called_once()

    def test_agent_api_rejects_a_held_registration(self):
        database = MagicMock()
        database.exists.return_value = True
        registration = FakeDoc(
            name="R1", docstatus=1, agent_retirement_hold=1
        )
        with patch.object(agent_sync.frappe, "db", database), patch.object(
            agent_sync.frappe, "get_doc", return_value=registration
        ), patch.object(
            agent_sync.frappe, "throw", side_effect=RuntimeError("blocked")
        ):
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                agent_sync._registration("R1")

    def test_lease_acquisition_checks_atomic_retirement_marker(self):
        cache = MagicMock()
        cache.eval.return_value = [0, b"retirement_hold", 30]
        doc = FakeDoc(name="R1")
        with patch.object(agent_sync, "_require_writer"), patch.object(
            agent_sync, "_registration", return_value=doc
        ), patch.object(
            agent_sync, "_validate_source", return_value="SOURCE"
        ), patch.object(
            agent_sync,
            "_config",
            return_value={
                "coordination_enabled": True,
                "lease_seconds": 300,
                "max_parallel_master_syncs": 2,
            },
        ), patch.object(
            agent_sync, "_lease_keys", return_value=(b"source", b"slots")
        ), patch.object(
            agent_sync, "_retirement_hold_key", return_value=b"hold"
        ), patch.object(agent_sync.frappe, "cache", cache):
            result = inspect.unwrap(agent_sync.acquire_sync_lease)(
                "R1", "SOURCE", "run-1", lease_token="token1"
            )
        self.assertFalse(result["acquired"])
        self.assertEqual(result["reason"], "retirement_hold")
        self.assertEqual(cache.eval.call_args.args[1:5], (3, b"source", b"slots", b"hold"))

    def test_persisted_hold_is_ready_only_after_the_source_lease_drains(self):
        registration = FakeDoc(
            name="R1",
            agent_retirement_hold=1,
            agent_retirement_hold_at="2026-11-05 12:00:00",
            agent_retirement_hold_by="Administrator",
            agent_sync_state="Succeeded",
        )
        meta = SimpleNamespace(has_field=lambda fieldname: True)
        with patch.object(
            retirement, "registration_source_key", return_value="SOURCE"
        ), patch.object(retirement.frappe, "get_meta", return_value=meta), patch(
            "db_connector.api_agent_sync.place_source_retirement_hold"
        ) as place_hold, patch(
            "db_connector.api_agent_sync.source_sync_lease_owner", return_value=""
        ):
            result = retirement._registration_retirement_readiness(registration)
        self.assertTrue(result["ready"])
        place_hold.assert_called_once_with("SOURCE", "R1")

    def test_cancellation_fails_before_deletion_without_the_hold(self):
        registration = FakeDoc(name="R1")
        flags = SimpleNamespace(
            ccd_registration_retirement_confirmation={
                "registration": "R1",
                "scope_fingerprint": "a" * 64,
                "reason": "replacement",
            }
        )
        with patch.object(retirement, "_require_manager"), patch.object(
            retirement.frappe, "flags", flags
        ), patch.object(
            retirement,
            "_require_registration_retirement_ready",
            side_effect=RuntimeError("hold missing"),
        ), patch.object(retirement, "_apply_source_retirement") as apply_retirement:
            with self.assertRaisesRegex(RuntimeError, "hold missing"):
                retirement.before_cancel_registration(registration)
        apply_retirement.assert_not_called()

    def test_inherited_timestamp_repair_is_narrow_and_idempotent(self):
        fields = [
            SimpleNamespace(fieldname=fieldname)
            for fieldname in agent_sync.AMENDED_OPERATIONAL_DEFAULTS
        ]
        rows = [
            FakeDoc(
                name="R1-1",
                creation="2026-11-03 16:47:51",
                agent_sync_started_at="2026-11-03 16:03:24",
                agent_sync_heartbeat_at="2026-11-03 16:40:37",
                agent_sync_finished_at="2026-11-03 16:40:37",
                last_heartbeat=None,
            ),
            FakeDoc(
                name="R2-1",
                creation="2026-11-03 16:47:51",
                agent_sync_started_at="2026-11-03 17:00:00",
                agent_sync_heartbeat_at="2026-11-03 17:01:00",
                agent_sync_finished_at="2026-11-03 17:02:00",
                last_heartbeat=None,
            ),
        ]
        database = MagicMock()
        with patch.object(
            agent_sync.frappe,
            "get_meta",
            return_value=SimpleNamespace(fields=fields),
        ), patch.object(agent_sync.frappe, "get_all", return_value=rows), patch.object(
            agent_sync.frappe, "db", database
        ):
            result = agent_sync.repair_inherited_amended_sync_state()
        self.assertEqual(result, {"updated": 1, "registrations": ["R1-1"]})
        self.assertEqual(database.set_value.call_count, 1)
        self.assertEqual(database.set_value.call_args.args[1], "R1-1")


if __name__ == "__main__":
    unittest.main()
