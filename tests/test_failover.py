

import pytest

from src.core.config import SETTINGS, replace
from src.core.errors import GenerationError, ModelUnavailable
from src.generation import client as llm


class _Stub:
    def __init__(self, name, fail=False, fail_after_first_chunk=False):
        self.name = name
        self.model = f"{name}-model"
        self.last_usage = (1, 2)
        self._fail = fail
        self._fail_late = fail_after_first_chunk
        self.calls = 0

    def generate(self, system, user):
        self.calls += 1
        if self._fail:
            raise GenerationError(f"{self.name} is down")
        return llm.Completion(f"answer from {self.name}", 1, 2)

    def stream(self, system, user):
        self.calls += 1
        if self._fail:
            raise GenerationError(f"{self.name} is down")
        yield f"hi from {self.name}"
        if self._fail_late:
            raise GenerationError(f"{self.name} died mid-stream")

    def health(self):
        if self._fail:
            raise ModelUnavailable(f"{self.name} is down")


def _chain(monkeypatch, *providers):
    registry = {p.name: p for p in providers}
    monkeypatch.setattr(llm, "_PROVIDERS", {n: (lambda n=n: registry[n]) for n in registry})
    return llm.FailoverProvider(list(registry))


def _breaker_settings(monkeypatch, failures=3, cooldown=60.0):
    monkeypatch.setattr(
        llm,
        "SETTINGS",
        replace(
            SETTINGS,
            models=replace(
                SETTINGS.models,
                llm_breaker_failures=failures,
                llm_breaker_cooldown_s=cooldown,
            ),
        ),
    )


def test_chain_puts_the_primary_first_and_drops_repeats(monkeypatch):
    monkeypatch.setattr(
        llm,
        "SETTINGS",
        replace(
            SETTINGS,
            models=replace(
                SETTINGS.models, llm_provider="gemini", llm_fallback_chain=("groq", "gemini")
            ),
        ),
    )
    assert llm.chain() == ["gemini", "groq"]


def test_generate_falls_through_to_the_next_provider(monkeypatch):
    _breaker_settings(monkeypatch)
    down, up = _Stub("gemini", fail=True), _Stub("groq")
    provider = _chain(monkeypatch, down, up)

    completion = provider.generate("sys", "user")

    assert completion.text == "answer from groq"
    # The served provider is what the trace and the cost metric are labelled with.
    assert provider.name == "groq"
    assert provider.model == "groq-model"
    assert provider.last_usage == (1, 2)


def test_stream_falls_over_before_the_first_chunk(monkeypatch):
    _breaker_settings(monkeypatch)
    provider = _chain(monkeypatch, _Stub("gemini", fail=True), _Stub("groq"))
    assert "".join(provider.stream("sys", "user")) == "hi from groq"


def test_stream_failure_after_the_first_chunk_propagates(monkeypatch):
    _breaker_settings(monkeypatch)
    provider = _chain(
        monkeypatch, _Stub("gemini", fail_after_first_chunk=True), _Stub("groq")
    )
    # Half an answer is already with the caller; restarting on another provider would restate it.
    with pytest.raises(GenerationError):
        list(provider.stream("sys", "user"))


def test_breaker_opens_and_stops_calling_the_dead_provider(monkeypatch):
    _breaker_settings(monkeypatch, failures=2, cooldown=60.0)
    down, up = _Stub("gemini", fail=True), _Stub("groq")
    provider = _chain(monkeypatch, down, up)

    for _ in range(4):
        provider.generate("sys", "user")

    assert down.calls == 2
    assert up.calls == 4


def test_breaker_lets_one_probe_through_after_the_cooldown(monkeypatch):
    _breaker_settings(monkeypatch, failures=1, cooldown=0.0)
    down, up = _Stub("gemini", fail=True), _Stub("groq")
    provider = _chain(monkeypatch, down, up)

    provider.generate("sys", "user")
    provider.generate("sys", "user")

    assert down.calls == 2


def test_recovery_closes_the_breaker(monkeypatch):
    _breaker_settings(monkeypatch, failures=1, cooldown=0.0)
    primary, backup = _Stub("gemini", fail=True), _Stub("groq")
    provider = _chain(monkeypatch, primary, backup)

    provider.generate("sys", "user")
    primary._fail = False
    assert provider.generate("sys", "user").text == "answer from gemini"
    assert provider.name == "gemini"
    # Closed again: a later blip starts from zero rather than one failure short of open.
    primary._fail = True
    provider.generate("sys", "user")
    primary._fail = False
    assert provider.generate("sys", "user").text == "answer from gemini"


def test_every_provider_down_raises_with_the_reasons(monkeypatch):
    _breaker_settings(monkeypatch)
    provider = _chain(monkeypatch, _Stub("gemini", fail=True), _Stub("groq", fail=True))
    with pytest.raises(ModelUnavailable) as exc:
        provider.generate("sys", "user")
    assert "gemini" in str(exc.value) and "groq" in str(exc.value)


def test_health_passes_when_any_member_is_usable(monkeypatch):
    _breaker_settings(monkeypatch)
    provider = _chain(monkeypatch, _Stub("gemini", fail=True), _Stub("groq"))
    provider.health()

    dead = _chain(monkeypatch, _Stub("gemini", fail=True), _Stub("groq", fail=True))
    with pytest.raises(ModelUnavailable):
        dead.health()


def test_a_single_provider_chain_is_not_wrapped(monkeypatch):
    provider = _chain(monkeypatch, _Stub("gemini"))
    monkeypatch.setattr(
        llm,
        "SETTINGS",
        replace(
            SETTINGS,
            models=replace(SETTINGS.models, llm_provider="gemini", llm_fallback_chain=()),
        ),
    )
    assert not isinstance(llm.build_client(), llm.FailoverProvider)
    assert isinstance(provider, llm.FailoverProvider)
