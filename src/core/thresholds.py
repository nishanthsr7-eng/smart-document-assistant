import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.errors import ConfigError
from src.core.flags import Flags

_ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "config" / "thresholds"

class TrustThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    abstain_threshold: float = Field(ge=0.0, le=1.0)
    abstain_soft_margin: float = Field(ge=0.0, le=1.0)
    min_source_score: float = Field(ge=0.0, le=1.0)
    support_threshold: float = Field(ge=0.0, le=1.0)
    grounding_threshold: float = Field(ge=0.0, le=1.0)
    confidence_high_edge: float = Field(ge=0.0, le=1.0)
    confidence_medium_edge: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _edges_are_ordered(self) -> "TrustThresholds":
        if self.confidence_medium_edge >= self.confidence_high_edge:
            raise ValueError("confidence_medium_edge must sit below confidence_high_edge")
        if self.abstain_threshold >= self.confidence_medium_edge:
            raise ValueError("abstain_threshold must sit below confidence_medium_edge")
        return self


class RetrievalThresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    calibration_logistic_a: float = Field(gt=0.0)
    calibration_logistic_b: float


class ThresholdArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    trust: TrustThresholds
    retrieval: RetrievalThresholds

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:12]


def _apply(payload: dict[str, Any], dotted: str, value: object) -> None:
    section, _, key = dotted.partition(".")
    if not key or section not in payload:
        raise ConfigError(f"THRESHOLD_OVERRIDES_JSON: '{dotted}' is not a path in the artifact")
    payload[section][key] = value


def load(flags: Flags | None = None) -> ThresholdArtifact:
    """Read the artifact the flags select and apply their overrides."""
    flags = flags or Flags()
    path = _ARTIFACT_DIR / f"{flags.thresholds_version}.yaml"
    if not path.is_file():
        raise ConfigError(f"No threshold artifact at {path}. See config/thresholds/.")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    for dotted, value in flags.threshold_overrides.items():
        _apply(payload, dotted, value)
    return ThresholdArtifact.model_validate(payload)
