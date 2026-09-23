from dataclasses import dataclass

from src.core.config import SETTINGS


@dataclass
class ConfidenceResult:
    label: str
    score: float
    components: dict[str, float]


def compute(
    top_score: float,
    margin: float,
    supported_fraction: float,
    agreement: float,
    has_conflict: bool,
    modifier: float = 1.0,
) -> ConfidenceResult:
    # Weights and edges are heuristic. Calibration: measure per-bucket accuracy on golden set
    # and fit logistic regression on signals if High-bucket accuracy < 80%.
    score = 0.35 * top_score + 0.15 * min(margin, 1.0) + 0.35 * supported_fraction + 0.15 * agreement
    if has_conflict:
        score *= 0.85
    score *= modifier
    score = max(0.0, min(1.0, score))

    cfg = SETTINGS.trust
    if score >= cfg.confidence_high_edge:
        label = "High"
    elif score >= cfg.confidence_medium_edge:
        label = "Medium"
    else:
        label = "Low"
    components = {
        "Retrieval score": top_score,
        "Top-1/top-2 margin": min(margin, 1.0),
        "Supported claims": supported_fraction,
        "Source agreement": agreement,
    }
    return ConfidenceResult(label, score, components)
