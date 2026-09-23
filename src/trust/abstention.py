from dataclasses import dataclass
from typing import Optional

from src.core.config import SETTINGS
from src.retrieval.vector_store import Hit


@dataclass
class AbstentionResult:
    should_abstain: bool
    reason: Optional[str]
    near_miss: Optional[Hit]
    confidence_modifier: float = 1.0


def check(hits: list[Hit], top_score: Optional[float], retrieval_consensus: bool = False) -> AbstentionResult:
    if not hits or top_score is None:
        return AbstentionResult(True, "No matching passages were found for this question.", None, 0.0)

    hard = SETTINGS.trust.abstain_threshold
    soft = hard + SETTINGS.trust.abstain_soft_margin

    if top_score < hard and not retrieval_consensus:
        return AbstentionResult(
            True,
            f"The best-matching passage scored {top_score:.2f}, below the trust threshold.",
            hits[0],
            0.0,
        )
    if top_score < soft and not retrieval_consensus:
        modifier = (top_score - hard) / (soft - hard)
        return AbstentionResult(False, None, None, modifier)
    return AbstentionResult(False, None, None, 1.0)
