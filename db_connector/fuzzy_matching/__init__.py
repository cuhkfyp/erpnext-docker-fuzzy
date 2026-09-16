"""Policy-driven cross-centre record linkage for CCD Master records."""

from .vendor import activate_vendor

activate_vendor()

from .models import EvaluationResult, compare_all_models
from .policy import MatchingPolicy, SourceProfile

__all__ = [
    "EvaluationResult",
    "MatchingPolicy",
    "SourceProfile",
    "compare_all_models",
]
