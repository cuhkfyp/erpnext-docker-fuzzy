"""Label-based evaluation and threshold selection without ML dependencies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True)
class BinaryMetrics:
    threshold: float
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class ThresholdSelection:
    high_threshold: float | None
    review_threshold: float | None
    high_metrics: BinaryMetrics | None
    review_metrics: BinaryMetrics | None
    warning: str | None = None


def binary_metrics(labels_scores: Iterable[tuple[bool, float]], threshold: float) -> BinaryMetrics:
    tp = fp = tn = fn = 0
    for label, score in labels_scores:
        predicted = score >= threshold
        if label and predicted:
            tp += 1
        elif not label and predicted:
            fp += 1
        elif not label:
            tn += 1
        else:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return BinaryMetrics(threshold, tp, fp, tn, fn, precision, recall, f1)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    radius = z * sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def select_thresholds(
    labels_scores: Iterable[tuple[bool, float]],
    *,
    high_precision_target: float = 0.95,
    minimum_high_samples: int = 30,
) -> ThresholdSelection:
    rows = list(labels_scores)
    if not rows:
        return ThresholdSelection(None, None, None, None, "no_adjudicated_labels")
    thresholds = sorted({score for _, score in rows})
    all_metrics = [binary_metrics(rows, threshold) for threshold in thresholds]

    high_candidates = [
        item
        for item in all_metrics
        if item.precision >= high_precision_target
        and item.true_positive + item.false_positive >= minimum_high_samples
    ]
    high = min(high_candidates, key=lambda item: item.threshold) if high_candidates else None
    review = max(all_metrics, key=lambda item: (item.f1, item.recall, item.precision, -item.threshold))
    warning = None if high else "high_tier_disabled_insufficient_precision_or_sample"
    return ThresholdSelection(
        high.threshold if high else None,
        review.threshold,
        high,
        review,
        warning,
    )


def cohens_kappa(labels: Iterable[tuple[str, str]]) -> float | None:
    rows = [(a, b) for a, b in labels if a and b]
    if not rows:
        return None
    categories = sorted({value for pair in rows for value in pair})
    observed = sum(a == b for a, b in rows) / len(rows)
    expected = 0.0
    for category in categories:
        left_rate = sum(a == category for a, _ in rows) / len(rows)
        right_rate = sum(b == category for _, b in rows) / len(rows)
        expected += left_rate * right_rate
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0
