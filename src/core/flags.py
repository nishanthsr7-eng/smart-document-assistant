import json
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Flags(BaseSettings):
    """Deploy-time selection layer over the threshold artifacts in config/thresholds/.

    Separate from Settings on purpose: which artifact a deploy runs, and any per-deploy
    deviation from it, is an operational decision that changes without a code change. The
    overrides are dotted paths into the artifact ("trust.abstain_threshold"), applied after it
    loads and validated against the same schema, so an override cannot smuggle in a key or a
    value the artifact itself could not hold.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    thresholds_version: str = Field(default="v1", validation_alias="THRESHOLDS_VERSION")
    threshold_overrides: dict[str, Any] = Field(
        default_factory=dict, validation_alias="THRESHOLD_OVERRIDES_JSON"
    )

    @field_validator("thresholds_version")
    @classmethod
    def _version_is_a_filename(cls, value: str) -> str:
        if not value.isalnum():
            raise ValueError("THRESHOLDS_VERSION must be alphanumeric, e.g. v1")
        return value

    @field_validator("threshold_overrides", mode="before")
    @classmethod
    def _parse_overrides(cls, value: object) -> object:
        if isinstance(value, str):
            return json.loads(value) if value.strip() else {}
        return value
