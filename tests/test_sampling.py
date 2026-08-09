import unittest

from fuzzy_matching.sampling import balanced_quotas, double_review_ids, stratified_sample, stratum
from fuzzy_matching.types import CandidatePair, EvaluationResult, MatchTier, ModelResult


def result(index: int, source_pair: str, score: float) -> EvaluationResult:
    pair = CandidatePair(f"L{index}", f"R{index}", source_pair, ("chi_full",))
    baseline = ModelResult("baseline", score, MatchTier.REVIEW)
    tiered = ModelResult("tiered", score, MatchTier.REVIEW)
    return EvaluationResult(pair, baseline, tiered, tiered)


class StreamingSamplingTests(unittest.TestCase):
    def test_streaming_sample_is_bounded_deterministic_and_covers_strata(self):
        rows = [
            result(index, f"S{index % 5}::T{index % 5}", (index % 10) / 10)
            for index in range(2_000)
        ]
        first = stratified_sample(iter(rows), 100, seed="run-1")
        second = stratified_sample(iter(rows), 100, seed="run-1")
        self.assertEqual(len(first), 100)
        self.assertEqual(
            [(item.pair.left_id, item.pair.right_id) for item in first],
            [(item.pair.left_id, item.pair.right_id) for item in second],
        )
        all_strata = {stratum(item) for item in rows}
        sampled_strata = {stratum(item) for item in first}
        self.assertEqual(sampled_strata, all_strata)

    def test_zero_sample_does_not_consume_generator(self):
        consumed = False

        def rows():
            nonlocal consumed
            consumed = True
            yield result(1, "A::B", 0.5)

        self.assertEqual(stratified_sample(rows(), 0, seed="run-2"), [])
        self.assertFalse(consumed)

    def test_sample_and_double_review_are_balanced_across_source_pairs(self):
        rows = [result(index, "A::B", 0.8) for index in range(900)]
        rows.extend(result(1_000 + index, "C::D", 0.8) for index in range(100))
        counts = {"A::B": 900, "C::D": 100}
        self.assertEqual(balanced_quotas(counts, 100), {"A::B": 50, "C::D": 50})
        sampled = stratified_sample(
            rows,
            100,
            seed="balanced-run",
            source_pair_counts=counts,
        )
        sample_counts = {
            source_pair: sum(item.pair.source_pair == source_pair for item in sampled)
            for source_pair in {item.pair.source_pair for item in sampled}
        }
        self.assertEqual(sample_counts, {"A::B": 50, "C::D": 50})

        doubles = double_review_ids(sampled, 20, seed="balanced-run")
        double_counts = {
            source_pair: sum(
                f"{item.pair.left_id}::{item.pair.right_id}" in doubles
                and item.pair.source_pair == source_pair
                for item in sampled
            )
            for source_pair in sample_counts
        }
        self.assertEqual(double_counts, {"A::B": 10, "C::D": 10})


if __name__ == "__main__":
    unittest.main()
