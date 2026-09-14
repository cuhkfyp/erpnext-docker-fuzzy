import json
import types
import unittest
from unittest.mock import patch

from fuzzy_matching import generation


class _FakeDB:
    def __init__(self):
        self.updates = []

    def sql(self, query, values):
        self.updates.append((" ".join(query.split()), tuple(values)))


class _FakeFrappe:
    def __init__(self, rows):
        self.rows = rows
        self.db = _FakeDB()

    def get_all(self, doctype, **kwargs):
        return list(self.rows.get(doctype, ()))


def _snapshot(*sources):
    return json.dumps(
        {"source_profiles": [{"source": source} for source in sources]}
    )


class GenerationReplacementTests(unittest.TestCase):
    def test_chunking_is_bounded_for_one_hundred_thousand_keys(self):
        chunks = list(generation._chunks(str(index) for index in range(100_000)))
        self.assertEqual(len(chunks), 100)
        self.assertTrue(all(len(chunk) == 1_000 for chunk in chunks))
        self.assertEqual(sum(map(len, chunks)), 100_000)

    def test_scope_is_order_independent_and_deduplicated(self):
        self.assertEqual(
            generation._source_scope(_snapshot("B", "A", "A")),
            ("A", "B"),
        )

    def test_canary_replacement_preserves_final_human_and_applied_states(self):
        current = types.SimpleNamespace(
            name="current", policy_snapshot_json=_snapshot("A", "B")
        )
        fake = _FakeFrappe(
            {
                "CCD Match Canary Run": [
                    types.SimpleNamespace(
                        name="prior-same", policy_snapshot_json=_snapshot("B", "A")
                    ),
                    types.SimpleNamespace(
                        name="prior-other", policy_snapshot_json=_snapshot("A", "C")
                    ),
                ],
                "CCD Match Recommendation": [
                    types.SimpleNamespace(name="R1", component_review="C1"),
                    types.SimpleNamespace(name="R2", component_review="C2"),
                ],
                "CCD Match Component Review": [
                    types.SimpleNamespace(
                        name="C1",
                        review_status="Agreed",
                        materialization_status="Applied",
                    ),
                    types.SimpleNamespace(
                        name="C2",
                        review_status="Pending",
                        materialization_status="Pending",
                    ),
                ],
            }
        )
        with patch.object(generation, "frappe", fake):
            summary = generation.supersede_prior_canary_generations(current)

        self.assertEqual(
            summary,
            {
                "superseded_canary_runs": 1,
                "superseded_recommendations": 2,
                "superseded_component_reviews": 2,
            },
        )
        rendered = "\n".join(
            f"{query} {values}" for query, values in fake.db.updates
        )
        self.assertNotIn("prior-other", rendered)
        self.assertIn("prior-same", rendered)
        self.assertIn("R1", rendered)
        self.assertIn("R2", rendered)
        c1_updates = [
            (query, values)
            for query, values in fake.db.updates
            if "tabCCD Match Component Review" in query and "C1" in values
        ]
        self.assertEqual(len(c1_updates), 1)
        self.assertNotIn("Stale", c1_updates[0][1])
        self.assertNotIn("Superseded", c1_updates[0][1])
        c2_updates = [
            values
            for query, values in fake.db.updates
            if "tabCCD Match Component Review" in query and "C2" in values
        ]
        self.assertEqual(len(c2_updates), 1)
        self.assertIn("Stale", c2_updates[0])
        self.assertIn("Superseded", c2_updates[0])

    def test_queue_replacement_preserves_final_review_but_supersedes_unapplied_work(self):
        current = types.SimpleNamespace(
            name="current", policy_snapshot_json=_snapshot("A", "B")
        )
        fake = _FakeFrappe(
            {
                "CCD Match Review Queue Run": [
                    types.SimpleNamespace(
                        name="prior", policy_snapshot_json=_snapshot("A", "B")
                    )
                ],
                "CCD Match Review Candidate": [
                    types.SimpleNamespace(
                        name="Q1",
                        review_status="Adjudicated",
                        materialization_status="Pending",
                        assigned_review_batch="B1",
                    ),
                    types.SimpleNamespace(
                        name="Q2",
                        review_status="Pending",
                        materialization_status="Pending",
                        assigned_review_batch="B1",
                    ),
                ],
            }
        )
        with patch.object(generation, "frappe", fake):
            summary = generation.supersede_prior_queue_generations(current)

        self.assertEqual(summary["superseded_queue_runs"], 1)
        self.assertEqual(summary["superseded_candidates"], 2)
        self.assertEqual(summary["stale_review_batches"], 1)
        q1_updates = [
            values
            for query, values in fake.db.updates
            if "tabCCD Match Review Candidate" in query and "Q1" in values
        ]
        self.assertEqual(len(q1_updates), 1)
        self.assertNotIn("Stale", q1_updates[0])
        self.assertIn("Superseded", q1_updates[0])
        rendered = "\n".join(
            f"{query} {values}" for query, values in fake.db.updates
        )
        self.assertIn("B1", rendered)
        self.assertIn("prior", rendered)


if __name__ == "__main__":
    unittest.main()
