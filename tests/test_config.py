"""Settings are read and validated when they are built, not when the module is imported.

The regression these guard is the old shape: env defaults evaluated at class-definition time, so
a bad value was whatever `int()` made of it and a change to the environment after import was
invisible. Every test here changes os.environ and rebuilds.
"""

import pytest

from src.core import thresholds
from src.core.config import SETTINGS, ModelConfig, build_settings, replace
from src.core.errors import ConfigError
from src.core.flags import Flags


@pytest.fixture
def env(monkeypatch):
    """The stack's own env, minus anything a test might be testing the absence of."""
    for name in ("THRESHOLDS_VERSION", "THRESHOLD_OVERRIDES_JSON", "NO_EGRESS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_environment_is_read_at_build_time_not_import_time(env):
    env.setenv("QUERY_CONCURRENCY", "3")
    assert build_settings().api.query_concurrency == 3
    env.setenv("QUERY_CONCURRENCY", "4")
    assert build_settings().api.query_concurrency == 4


def test_an_out_of_range_value_fails_the_build(env):
    env.setenv("OTEL_TRACE_SAMPLE_RATIO", "7")
    with pytest.raises(ConfigError, match="OTEL_TRACE_SAMPLE_RATIO"):
        build_settings()


def test_an_unparseable_value_fails_the_build(env):
    env.setenv("QUERY_CONCURRENCY", "eight")
    with pytest.raises(ConfigError, match="QUERY_CONCURRENCY"):
        build_settings()


def test_an_unknown_provider_fails_the_build(env):
    env.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="llm_provider"):
        build_settings()


def test_an_unknown_provider_in_the_fallback_chain_fails_the_build(env):
    env.setenv("LLM_FALLBACK_CHAIN", "groq,openai")
    with pytest.raises(ConfigError, match="unknown providers"):
        build_settings()


def test_a_wildcard_cors_origin_is_refused(env):
    env.setenv("CORS_ALLOW_ORIGINS", "*")
    with pytest.raises(ConfigError, match="cannot be"):
        build_settings()


def test_cors_origins_are_a_comma_separated_list(env):
    env.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com, https://admin.example.com")
    origins = build_settings().security.cors_allow_origins
    assert origins == ("https://app.example.com", "https://admin.example.com")


def test_no_egress_refuses_to_start_with_a_hosted_provider(env):
    env.setenv("NO_EGRESS", "1")
    env.setenv("LLM_PROVIDER", "gemini")
    with pytest.raises(ConfigError, match="hosted generation provider"):
        build_settings()


def test_no_egress_starts_with_a_local_provider_and_no_exporters(env):
    env.setenv("NO_EGRESS", "1")
    env.setenv("LLM_PROVIDER", "ollama")
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "LANGFUSE_PUBLIC_KEY", "SENTRY_DSN"):
        env.setenv(name, "")
    assert build_settings().security.no_egress


def test_thresholds_come_from_the_selected_artifact(env):
    artifact = thresholds.load(Flags())
    assert build_settings().trust == artifact.trust
    assert build_settings().retrieval.calibration_logistic_a == (
        artifact.retrieval.calibration_logistic_a
    )


def test_a_missing_artifact_version_fails_the_build(env):
    env.setenv("THRESHOLDS_VERSION", "v99")
    with pytest.raises(ConfigError, match="No threshold artifact"):
        build_settings()


def test_a_flag_override_moves_a_threshold_and_the_cache_key(env):
    before = build_settings()
    env.setenv("THRESHOLD_OVERRIDES_JSON", '{"trust.abstain_threshold": 0.42}')
    after = build_settings()
    assert after.trust.abstain_threshold == 0.42
    # The answer cache must not keep serving answers decided under the old threshold.
    assert after.retrieval_version != before.retrieval_version


def test_an_override_of_a_path_the_artifact_does_not_have_is_refused(env):
    env.setenv("THRESHOLD_OVERRIDES_JSON", '{"trust.made_up": 1}')
    with pytest.raises(ConfigError, match="made_up"):
        build_settings()


def test_an_override_outside_the_artifact_schema_is_refused(env):
    env.setenv("THRESHOLD_OVERRIDES_JSON", '{"trust.abstain_threshold": 1.5}')
    with pytest.raises(ConfigError, match="abstain_threshold"):
        build_settings()


def test_confidence_edges_must_stay_ordered(env):
    env.setenv("THRESHOLD_OVERRIDES_JSON", '{"trust.confidence_medium_edge": 0.9}')
    with pytest.raises(ConfigError, match="below confidence_high_edge"):
        build_settings()


def test_replace_validates_what_it_is_given():
    assert replace(SETTINGS.models, llm_provider="ollama").llm_provider == "ollama"
    with pytest.raises(ValueError, match="llm_provider"):
        replace(SETTINGS.models, llm_provider="openai")


def test_replace_does_not_re_read_the_environment(env):
    env.setenv("LLM_PROVIDER", "groq")
    assert replace(ModelConfig(llm_provider="ollama"), ollama_model="q").llm_provider == "ollama"
