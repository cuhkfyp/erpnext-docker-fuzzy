"""Shared immutable values used by the matching engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EvidenceLevel(str, Enum):
    MISSING = "missing"
    DISAGREE = "disagree"
    WEAK = "weak"
    CLOSE = "close"
    PHONETIC = "phonetic"
    EXACT = "exact"


class MatchTier(str, Enum):
    HIGH = "high"
    REVIEW = "review"
    CONFLICT = "conflict_review"
    LOW = "low_insufficient"


@dataclass(frozen=True)
class Evidence:
    attribute: str
    level: EvidenceLevel
    score: float
    left_present: bool
    right_present: bool
    reason: str

    @property
    def available(self) -> bool:
        return self.left_present and self.right_present

    @property
    def exact(self) -> bool:
        return self.level == EvidenceLevel.EXACT


@dataclass(frozen=True)
class ModelResult:
    model: str
    score: float | None
    tier: MatchTier
    reasons: tuple[str, ...] = ()
    evidence: dict[str, Evidence] = field(default_factory=dict)
    probability: float | None = None


@dataclass(frozen=True)
class CandidatePair:
    left_id: str
    right_id: str
    source_pair: str
    blocking_routes: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationResult:
    pair: CandidatePair
    baseline: ModelResult
    tiered_gated: ModelResult
    tiered_recoverable: ModelResult
    probabilistic: ModelResult | None = None
    hybrid: ModelResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
