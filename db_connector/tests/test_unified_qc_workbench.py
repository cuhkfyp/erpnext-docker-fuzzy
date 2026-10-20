import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from db_connector.db_connector.report.ccd_unified_qc_workbench import (
    ccd_unified_qc_workbench as report,
)


class UnifiedQcWorkbenchTests(unittest.TestCase):
    def setUp(self):
        translations = patch.object(report, "_", side_effect=lambda value: value)
        translations.start()
        self.addCleanup(translations.stop)

    def test_enrich_case_identifies_current_reviewers_next_action(self):
        case = {
            "review_status": "Partially Reviewed",
            "stale": 0,
            "due_at": datetime(2026, 11, 2),
        }
        labels = [{"reviewer": "first@example.com", "is_adjudication": 0}]

        first = report._enrich_case(
            case,
            labels,
            current_user="first@example.com",
            can_adjudicate=False,
            current_time=datetime(2026, 10, 20),
        )
        second = report._enrich_case(
            case,
            labels,
            current_user="second@example.com",
            can_adjudicate=False,
            current_time=datetime(2026, 10, 20),
        )

        self.assertEqual(first["my_action"], "Awaiting another reviewer")
        self.assertEqual(second["my_action"], "Review now")
        self.assertEqual(second["review_progress"], "1 / 2")

    def test_enrich_case_routes_disagreement_to_manager_adjudication(self):
        case = {
            "review_status": "Needs Adjudication",
            "stale": 0,
            "due_at": datetime(2026, 10, 19),
        }
        labels = [
            {"reviewer": "first@example.com", "is_adjudication": 0},
            {"reviewer": "second@example.com", "is_adjudication": 0},
        ]

        row = report._enrich_case(
            case,
            labels,
            current_user="manager@example.com",
            can_adjudicate=True,
            current_time=datetime(2026, 10, 20),
        )

        self.assertEqual(row["my_action"], "Adjudicate")
        self.assertTrue(row["overdue"])
        self.assertEqual(row["review_progress"], "2 / 2")

    def test_unassigned_cases_are_not_part_of_the_report_query(self):
        with patch.object(report.frappe, "get_all", side_effect=[[], []]) as get_all:
            self.assertEqual(report._assigned_cases("active-canary"), [])

        self.assertEqual(
            get_all.call_args_list[0].kwargs["filters"],
            {"qc_selected": 1, "qc_assigned_at": ["is", "set"]},
        )
        self.assertEqual(
            get_all.call_args_list[1].kwargs["filters"],
            {"automated_qc_assigned_at": ["is", "set"]},
        )

    def test_report_defines_all_four_operational_channels(self):
        self.assertEqual(
            report.CHANNELS,
            {
                "Tiered — Active Canary",
                "Tiered — Mandatory Backlog",
                "Ultra-high Splink",
                "Development Hybrid",
            },
        )

    def test_assigned_cases_separate_active_backlog_splink_and_hybrid(self):
        def recommendation(name, canary):
            return SimpleNamespace(
                name=name,
                canary_run=canary,
                source_pair="A::B",
                activation_batch=None,
                qc_assigned_at=datetime(2026, 10, 19),
                qc_due_at=datetime(2026, 11, 2),
                qc_review_status="Unreviewed",
                qc_final_label="",
                qc_stale=0,
            )

        def candidate(name, channel):
            return SimpleNamespace(
                name=name,
                source_pair="A::B",
                automation_channel=channel,
                automation_validation_run="validation",
                automation_cohort="Scheduled Splink",
                automation_batch="batch",
                automated_qc_assigned_at=datetime(2026, 10, 19),
                automated_qc_due_at=datetime(2026, 11, 2),
                automated_qc_review_status="Unreviewed",
                automated_qc_final_label="",
                automated_qc_stale=0,
            )

        query_results = [
            [recommendation("active", "active-canary"), recommendation("old", "old-canary")],
            [
                SimpleNamespace(name="active-canary", status="Active"),
                SimpleNamespace(name="old-canary", status="Superseded"),
            ],
            [candidate("splink", ""), candidate("hybrid", "Hybrid Development")],
        ]
        with patch.object(report.frappe, "get_all", side_effect=query_results):
            rows = report._assigned_cases("active-canary")

        self.assertEqual(
            {row["qc_case"]: row["channel"] for row in rows},
            {
                "active": "Tiered — Active Canary",
                "old": "Tiered — Mandatory Backlog",
                "splink": "Ultra-high Splink",
                "hybrid": "Development Hybrid",
            },
        )


if __name__ == "__main__":
    unittest.main()
