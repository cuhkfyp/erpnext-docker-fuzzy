import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fuzzy_matching.policy import MatchingPolicy, SourceProfile


class ReviewQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_root = str(Path(__file__).resolve().parents[2])
        if app_root not in sys.path:
            sys.path.insert(0, app_root)
        fake_frappe = types.SimpleNamespace(whitelist=lambda: (lambda function: function))
        sys.modules.setdefault("frappe", fake_frappe)
        cls.module = importlib.import_module("db_connector.api_fuzzy_review_queue")

    def test_queue_uses_evaluation_splink_normalization(self):
        policy = MatchingPolicy(
            source_profiles={
                "A": SourceProfile("A", {}),
                "B": SourceProfile("B", {}),
            }
        )
        records = [
            {
                "record_id": "A1",
                "source": "A",
                "chi_surname": " 陳 ",
                "chi_firstname": " 大 文 ",
                "eng_surname": "  CHAN ",
                "eng_firstname": " Tai   Man ",
                "birthday": "1980-01-02",
                "phone": "9123 4567",
                "email": "USER@EXAMPLE.COM",
            },
            {
                "record_id": "B1",
                "source": "B",
                "chi_surname": "陳",
                "chi_firstname": "大文",
                "eng_surname": "Chan",
                "eng_firstname": "Tai Man",
                "birthday": "1980-01-02",
                "phone": "91234567",
                "email": "user@example.com",
            },
            {"record_id": "C1", "source": "A"},
        ]

        training, scoring = self.module._queue_splink_records(
            records,
            policy,
            {"A1", "B1"},
            {("A1", "B1")},
            training_record_limit=2,
        )

        self.assertEqual({row["record_id"] for row in training}, {"A1", "B1"})
        self.assertEqual({row["record_id"] for row in scoring}, {"A1", "B1"})
        first = next(row for row in scoring if row["record_id"] == "A1")
        second = next(row for row in scoring if row["record_id"] == "B1")
        self.assertEqual(first["chi_full"], second["chi_full"])
        self.assertNotIn(" ", first["chi_full"])
        self.assertEqual(first["eng_full"], "chan tai man")
        self.assertEqual(first["phone"], "91234567")
        self.assertEqual(first["email"], "user@example.com")

    def test_approved_runtime_uses_frozen_evaluation_limits(self):
        evaluation = types.SimpleNamespace(
            model_versions_json=json.dumps(
                {
                    "splink": {"duckdb": "1", "splink": "4"},
                    "splink_adapter": self.module.SPLINK_ADAPTER_VERSION,
                    "splink_status": "local",
                    "splink_warning": None,
                    "splink_random_match_prior": self.module.RANDOM_MATCH_PRIOR,
                    "splink_training_record_limit": 5_000,
                    "splink_training_candidate_pair_limit": 250_000,
                    "splink_u_random_pair_limit": 250_000,
                    "splink_u_random_seed": self.module.U_RANDOM_SEED,
                }
            )
        )
        with patch.object(
            self.module,
            "dependency_versions",
            return_value={"duckdb": "1", "splink": "4"},
        ):
            runtime = self.module._approved_splink_runtime(evaluation)

        self.assertEqual(
            runtime,
            {
                "training_record_limit": 5_000,
                "training_candidate_pair_limit": 250_000,
                "u_random_pair_limit": 250_000,
                "u_random_seed": self.module.U_RANDOM_SEED,
            },
        )

    def test_probability_replay_checks_stored_scores_with_frozen_limits(self):
        policy = MatchingPolicy(
            source_profiles={
                "A": SourceProfile("A", {}),
                "B": SourceProfile("B", {}),
            },
            max_candidate_pairs=1_000_000,
        )
        records = [
            {"record_id": "A1", "source": "A"},
            {"record_id": "B1", "source": "B"},
        ]
        stored = [
            types.SimpleNamespace(
                left_record="A1",
                right_record="B1",
                probabilistic_score=0.75,
            )
        ]
        predictions = [
            types.SimpleNamespace(
                left_id="B1",
                right_id="A1",
                probability=0.75 + 1e-12,
            )
        ]
        captured = {}

        def fake_score(training_records, scoring_records, pairs, **kwargs):
            captured["pairs"] = pairs
            captured.update(kwargs)
            return predictions

        with patch.object(
            self.module.frappe,
            "get_all",
            return_value=stored,
            create=True,
        ), patch.object(self.module, "score_requested_pairs", side_effect=fake_score):
            result = self.module._replay_approved_probability_scores(
                "evaluation-1",
                records,
                policy,
                [{"record_id": "A1"}, {"record_id": "B1"}],
                {
                    "training_record_limit": 5_000,
                    "training_candidate_pair_limit": 250_000,
                    "u_random_pair_limit": 123_456,
                    "u_random_seed": 7,
                },
            )

        self.assertTrue(result["passed"])
        self.assertEqual(captured["pairs"], {("A1", "B1")})
        self.assertEqual(captured["minimum_probability"], -1.0)
        self.assertEqual(captured["max_prediction_pairs"], 250_000)
        self.assertEqual(captured["u_random_max_pairs"], 123_456)
        self.assertEqual(captured["u_random_seed"], 7)


if __name__ == "__main__":
    unittest.main()
