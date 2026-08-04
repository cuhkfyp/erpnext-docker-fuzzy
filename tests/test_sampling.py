import unittest

from fuzzy_matching.sampling import stratified_sample, stratum
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


if __name__ == "__main__":
    unittest.main()
