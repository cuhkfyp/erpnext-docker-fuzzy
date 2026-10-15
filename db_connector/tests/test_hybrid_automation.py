import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from db_connector import api_hybrid_automation as service
from fuzzy_matching.hybrid_automation import bounded_hybrid_pairs


class HybridAutomationSelectionTests(unittest.TestCase):
    def test_limit_is_per_cycle_and_selection_is_stable(self):
        rows = [
            {"name": "c", "source_pair": "A::B", "probabilistic_score": 0.97},
            {"name": "b", "source_pair": "A::B", "probabilistic_score": 0.98},
            {"name": "a", "source_pair": "A::B", "probabilistic_score": 0.98},
        ]
        selected = bounded_hybrid_pairs(
            reversed(rows), component_limit=2, allowed_sources=["A::B"]
        )
        self.assertEqual([row["name"] for row in selected], ["a", "b"])

    def test_frozen_source_scope_is_enforced(self):
        rows = [
            {"name": "in", "source_pair": "A::B", "probabilistic_score": 0.96},
            {"name": "out", "source_pair": "C::D", "probabilistic_score": 0.99},
        ]
        selected = bounded_hybrid_pairs(
            rows, component_limit=10, allowed_sources=["A::B"]
        )
        self.assertEqual([row["name"] for row in selected], ["in"])

    def test_helper_returns_the_requested_deterministic_slice(self):
        rows = [
            {"name": str(index), "source_pair": "A::B", "probabilistic_score": 0.96}
            for index in range(200)
        ]
        self.assertEqual(
            len(
                bounded_hybrid_pairs(
                    rows, component_limit=150, allowed_sources=["A::B"]
                )
            ),
            150,
        )


class HybridAutomationAuthorizationTests(unittest.TestCase):
    def _run(self):
        return types.SimpleNamespace(
            name="hybrid-run",
            run_purpose="Hybrid Prospective Validation",
            hybrid_development_only=1,
            status="Completed",
            approval_status="Approved",
            hybrid_holdout_status="Completed",
            hybrid_second_approval_status="Approved",
            authorization_stale=0,
            hybrid_holdout_metrics_json=json.dumps(
                {"combined_authorization_gate": {"passed": True}}
            ),
            validation_queue_run="queue",
            matching_policy="policy",
            hybrid_rule_version="hybrid-research-v1",
            authorization_fingerprint="fingerprint",
            hybrid_minimum_score=0.95,
            frozen_automatic_cutoff=0.99914814,
            source_scope_json=json.dumps(["A::B"]),
            hybrid_route_scope_json=json.dumps(["route"]),
        )

    def _settings(self):
        return types.SimpleNamespace(
            authorized_hybrid_validation_run="hybrid-run",
            authorized_hybrid_queue="queue",
            authorized_hybrid_policy="policy",
            authorized_hybrid_rule_version="hybrid-research-v1",
            authorized_hybrid_fingerprint="fingerprint",
            authorized_hybrid_minimum_score=0.95,
            authorized_hybrid_upper_cutoff=0.99914814,
            authorized_hybrid_source_scope_json=json.dumps(["A::B"]),
            authorized_hybrid_route_scope_json=json.dumps(["route"]),
        )

    def test_complete_second_approval_and_exact_provenance_pass(self):
        with patch.object(service, "_provenance_reasons", return_value=[]):
            self.assertEqual(
                service._authorization_blockers(self._settings(), self._run()), []
            )

    def test_source_scope_change_fails_closed(self):
        settings = self._settings()
        settings.authorized_hybrid_source_scope_json = json.dumps(["C::D"])
        with patch.object(service, "_provenance_reasons", return_value=[]):
            blockers = service._authorization_blockers(settings, self._run())
        self.assertIn("authorized_hybrid_source_scope_mismatch", blockers)

    def test_missing_second_approval_fails_closed(self):
        run = self._run()
        run.hybrid_second_approval_status = "Pending Management Review"
        with patch.object(service, "_provenance_reasons", return_value=[]):
            blockers = service._authorization_blockers(self._settings(), run)
        self.assertIn("hybrid_holdout_second_approval_missing", blockers)


class HybridAutomationBatchStatusTests(unittest.TestCase):
    def test_live_batch_status_is_returned_for_desk_polling(self):
        batch = types.SimpleNamespace(
            name="hybrid-batch",
            batch_type=service.HYBRID_BATCH_TYPE,
            status="Applied with Exceptions",
            component_count=150,
            applied_count=149,
            exception_count=1,
            stale_count=0,
            error_summary="",
            applied_at="2026-10-15 01:00:00",
        )
        with patch.object(service, "_require_manager"), patch.object(
            service.frappe, "get_doc", return_value=batch
        ):
            result = service.get_hybrid_batch_status.__wrapped__(batch.name)
        self.assertEqual(result["status"], "Applied with Exceptions")
        self.assertEqual(result["selected_components"], 150)
        self.assertEqual(result["applied"], 149)
        self.assertEqual(result["exceptions"], 1)


class HybridAutomationDeskContractTests(unittest.TestCase):
    def setUp(self):
        self.app = Path(__file__).resolve().parents[1]

    def test_controls_are_default_off_and_expose_a_per_cycle_limit(self):
        schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.json"
            ).read_text(encoding="utf-8")
        )
        fields = {field["fieldname"]: field for field in schema["fields"]}
        self.assertEqual(fields["automatic_hybrid_enabled"]["default"], "0")
        self.assertEqual(
            fields["automatic_hybrid_scheduling_enabled"]["default"], "0"
        )
        self.assertEqual(fields["automatic_hybrid_components_per_run"]["default"], "1")
        self.assertTrue(
            {
                "authorized_hybrid_validation_run",
                "authorized_hybrid_source_scope_json",
                "authorized_hybrid_route_scope_json",
                "hybrid_automation_paused",
                "last_hybrid_automation_batch",
            }.issubset(fields)
        )

    def test_desk_exposes_authorize_preview_and_manual_cycle(self):
        source = (
            self.app
            / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.js"
        ).read_text(encoding="utf-8")
        self.assertIn("Authorize Approved Hybrid Evidence", source)
        self.assertIn("Preview Development Hybrid Cycle", source)
        self.assertIn("Run One Development Hybrid Cycle Now", source)
        self.assertIn("get_hybrid_batch_status", source)
        self.assertIn("poll_hybrid_batch_status", source)
        self.assertIn('${__("Exceptions")}: ${esc(String(result.exceptions ?? 0))}', source)
        self.assertIn('${__("Stale")}: ${esc(String(result.stale ?? 0))}', source)
        self.assertIn("automatic_hybrid_components_per_run", json.dumps(
            json.loads((self.app / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.json").read_text())
        ))

    def test_hybrid_limit_is_manager_bounded_without_a_hidden_100_clamp(self):
        automation = (self.app / "api_hybrid_automation.py").read_text(encoding="utf-8")
        self.assertIn(
            "limit = max(int(settings.automatic_hybrid_components_per_run or 1), 1)",
            automation,
        )
        self.assertNotIn(
            "min(max(int(settings.automatic_hybrid_components_per_run or 1), 1), 100)",
            automation,
        )
        schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_identity_resolution_settings/ccd_identity_resolution_settings.json"
            ).read_text(encoding="utf-8")
        )
        field = next(
            item
            for item in schema["fields"]
            if item["fieldname"] == "automatic_hybrid_components_per_run"
        )
        self.assertNotIn("1-100", field["description"])

    def test_applied_hybrid_enters_sampling_pool_not_mandatory_qc(self):
        source = (self.app / "api_hybrid_automation.py").read_text(encoding="utf-8")
        self.assertIn(
            '"automated_qc_review_status": "Sampling Eligible"', source
        )
        schema = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_match_review_candidate/ccd_match_review_candidate.json"
            ).read_text(encoding="utf-8")
        )
        status = next(
            field
            for field in schema["fields"]
            if field["fieldname"] == "automated_qc_review_status"
        )
        self.assertIn("Sampling Eligible", status["options"].splitlines())
        self.assertIn("Not Selected", status["options"].splitlines())

    def test_identity_and_batch_provenance_are_explicit(self):
        decision = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_identity_decision/ccd_identity_decision.json"
            ).read_text(encoding="utf-8")
        )
        origin = next(
            field for field in decision["fields"] if field["fieldname"] == "origin"
        )
        self.assertIn("Hybrid Automated", origin["options"].splitlines())
        batch = json.loads(
            (
                self.app
                / "db_connector/doctype/ccd_splink_automation_batch/ccd_splink_automation_batch.json"
            ).read_text(encoding="utf-8")
        )
        batch_type = next(
            field for field in batch["fields"] if field["fieldname"] == "batch_type"
        )
        self.assertIn("Development Hybrid Automation", batch_type["options"].splitlines())


if __name__ == "__main__":
    unittest.main()
