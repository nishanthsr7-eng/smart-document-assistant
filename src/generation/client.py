import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Protocol, TypeVar

import httpx

from src.core import logs, metrics
from src.core.config import SETTINGS
from src.core.errors import GenerationError, ModelUnavailable

logger = logs.logger(__name__)

_T = TypeVar("_T")
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4


def _retry_transient(call: Callable[[], _T], status_of: Callable[[Exception], int | None]) -> _T:
    for attempt in range(_MAX_RETRIES):
        try:
            return call()
        except Exception as exc:
            if status_of(exc) not in _TRANSIENT_STATUS:
                raise
            time.sleep(2**attempt + random.uniform(0, 1))
    return call()


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int


def _completed(provider: str, model: str, text: str, prompt: int, completion: int) -> Completion:
    """Every provider call is metered here, not at the call sites: query condensing, expansion
    and follow-up suggestions spend tokens too, and a cost dashboard that missed them would lie."""
    metrics.record_tokens(provider, model, prompt, completion)
    return Completion(text, prompt, completion)


def _record_stream(provider: str, model: str, usage: tuple[int, int]) -> None:
    metrics.record_tokens(provider, model, usage[0], usage[1])


class Provider(Protocol):
    name: str
    model: str
    last_usage: tuple[int, int]

    def stream(self, system: str, user: str) -> Iterator[str]: ...

    def generate(self, system: str, user: str) -> Completion: ...

    def health(self) -> None: ...


class OllamaProvider:
    name = "ollama"

    def __init__(self) -> None:
        from ollama import Client

        self.model = SETTINGS.models.ollama_model
        self._client = Client(host=SETTINGS.models.ollama_host)
        self.last_usage = (0, 0)

    def stream(self, system: str, user: str) -> Iterator[str]:
        cfg = SETTINGS.generation
        try:
            for part in self._client.chat(
                model=self.model,
                messages=_messages(system, user),
                think=False,
                stream=True,
                options={"temperature": cfg.temperature, "num_ctx": cfg.num_ctx},
                keep_alive=cfg.keep_alive,
            ):
                if part.get("done"):
                    self.last_usage = (part.get("prompt_eval_count", 0), part.get("eval_count", 0))
                chunk = part["message"]["content"]
                if chunk:
                    yield chunk
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            raise ModelUnavailable(
                f"Lost the connection to Ollama at {SETTINGS.models.ollama_host}."
            ) from exc
        _record_stream(self.name, self.model, self.last_usage)

    def generate(self, system: str, user: str) -> Completion:
        cfg = SETTINGS.generation
        try:
            res = self._client.chat(
                model=self.model,
                messages=_messages(system, user),
                think=False,
                options={"temperature": cfg.temperature, "num_ctx": cfg.num_ctx},
                keep_alive=cfg.keep_alive,
            )
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            raise ModelUnavailable(
                f"Lost the connection to Ollama at {SETTINGS.models.ollama_host}."
            ) from exc
        return _completed(
            self.name,
            self.model,
            res["message"]["content"],
            res.get("prompt_eval_count", 0),
            res.get("eval_count", 0),
        )

    def health(self) -> None:
        from ollama import ResponseError

        try:
            tags = self._client.list()
        except (httpx.ConnectError, ResponseError) as exc:
            raise ModelUnavailable(
                f"Can't reach the Ollama server at {SETTINGS.models.ollama_host}. "
                "Start it with the project's 'ollama' launch config."
            ) from exc
        names = {m["model"] for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise ModelUnavailable(f"Model '{self.model}' isn't pulled on the Ollama server.")


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        from google import genai

        key = SETTINGS.models.gemini_api_key
        if not key:
            raise ModelUnavailable("GEMINI_API_KEY is not set. Add it to .env or switch LLM_PROVIDER.")
        self.model = SETTINGS.models.gemini_model
        self._client = genai.Client(api_key=key)
        self.last_usage = (0, 0)

    def _config(self, system: str):
        from google.genai import types

        cfg = SETTINGS.generation
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=cfg.temperature,
            max_output_tokens=cfg.max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def stream(self, system: str, user: str) -> Iterator[str]:
        try:
            for chunk in self._client.models.generate_content_stream(
                model=self.model, contents=user, config=self._config(system)
            ):
                usage = chunk.usage_metadata
                if usage is not None:
                    self.last_usage = (
                        getattr(usage, "prompt_token_count", 0) or 0,
                        getattr(usage, "candidates_token_count", 0) or 0,
                    )
                if chunk.text:
                    yield chunk.text
        except Exception as exc:
            raise GenerationError(f"Gemini request failed: {exc}") from exc
        _record_stream(self.name, self.model, self.last_usage)

    def generate(self, system: str, user: str) -> Completion:
        from google.genai import errors

        def call():
            return self._client.models.generate_content(
                model=self.model, contents=user, config=self._config(system)
            )

        def status_of(exc: Exception) -> int | None:
            return exc.code if isinstance(exc, errors.APIError) else None

        try:
            res = _retry_transient(call, status_of)
        except Exception as exc:
            raise GenerationError(f"Gemini request failed: {exc}") from exc
        usage = res.usage_metadata
        return _completed(
            self.name,
            self.model,
            res.text or "",
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
        )

    def health(self) -> None:
        if not SETTINGS.models.gemini_api_key:
            raise ModelUnavailable("GEMINI_API_KEY is not set.")


class GroqProvider:
    name = "groq"

    def __init__(self) -> None:
        from groq import Groq

        key = SETTINGS.models.groq_api_key
        if not key:
            raise ModelUnavailable("GROQ_API_KEY is not set. Add it to .env or switch LLM_PROVIDER.")
        self.model = SETTINGS.models.groq_model
        self._client: Any = Groq(api_key=key)
        self.last_usage = (0, 0)

    def stream(self, system: str, user: str) -> Iterator[str]:
        cfg = SETTINGS.generation
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=_messages(system, user),
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
            for event in stream:
                if event.usage is not None:
                    self.last_usage = (event.usage.prompt_tokens, event.usage.completion_tokens)
                if not event.choices:
                    continue
                chunk = event.choices[0].delta.content
                if chunk:
                    yield chunk
        except Exception as exc:
            raise GenerationError(f"Groq request failed: {exc}") from exc
        _record_stream(self.name, self.model, self.last_usage)

    def generate(self, system: str, user: str) -> Completion:
        import groq

        cfg = SETTINGS.generation

        def call():
            return self._client.chat.completions.create(
                model=self.model,
                messages=_messages(system, user),
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
            )

        def status_of(exc: Exception) -> int | None:
            return exc.status_code if isinstance(exc, groq.APIStatusError) else None

        try:
            res = _retry_transient(call, status_of)
        except Exception as exc:
            raise GenerationError(f"Groq request failed: {exc}") from exc
        usage = res.usage
        return _completed(
            self.name,
            self.model,
            res.choices[0].message.content or "",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )

    def health(self) -> None:
        if not SETTINGS.models.groq_api_key:
            raise ModelUnavailable("GROQ_API_KEY is not set.")


class NullProvider:
    """Retrieval-only provider: no network, no key, no generation. `generate` returns empty text,
    which `query.condense`/`expand_query` already fall back from, so retrieval runs unexpanded
    instead of silently inventing variants. Used by the CI eval gate, where no API key exists."""

    name = "none"
    model = "none"

    def __init__(self) -> None:
        self.last_usage = (0, 0)

    def stream(self, system: str, user: str) -> Iterator[str]:
        raise ModelUnavailable("LLM_PROVIDER=none cannot generate answers.")

    def generate(self, system: str, user: str) -> Completion:
        return Completion("", 0, 0)

    def health(self) -> None:
        return None


_PROVIDERS: dict[str, Callable[[], Provider]] = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "none": NullProvider,
}


class _Breaker:
    """One provider's failure state. Closed, or open until a cooldown elapses and one probe is
    let through: a provider that is down should cost one request per cooldown, not every request.
    """

    def __init__(self, provider: str) -> None:
        self._provider = provider
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        metrics.LLM_BREAKER_OPEN.labels(provider).set(0)

    def allows(self) -> bool:
        cfg = SETTINGS.models
        with self._lock:
            if self._failures < cfg.llm_breaker_failures:
                return True
            # Open. The probe resets the clock, so concurrent callers do not all probe at once.
            if time.monotonic() - self._opened_at < cfg.llm_breaker_cooldown_s:
                return False
            self._opened_at = time.monotonic()
            return True

    def succeeded(self) -> None:
        with self._lock:
            self._failures = 0
        metrics.LLM_BREAKER_OPEN.labels(self._provider).set(0)

    def failed(self) -> None:
        with self._lock:
            self._failures += 1
            opened = self._failures >= SETTINGS.models.llm_breaker_failures
            if opened:
                self._opened_at = time.monotonic()
        if opened:
            metrics.LLM_BREAKER_OPEN.labels(self._provider).set(1)


class FailoverProvider:
    """An ordered chain of providers behind one Provider interface.

    A generation is attempted against each member whose breaker allows it, in order, until one
    answers. Construction counts as an attempt: a missing API key trips that member's breaker
    rather than failing the process, which is what makes the chain survive a key being rotated
    out from under a running deployment.

    `stream` fails over only before the first chunk reaches the caller. Once tokens are out, a
    second provider would restate the answer from the top, so a mid-stream failure propagates.
    """

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self._built: dict[str, Provider] = {}
        self._breakers = {name: _Breaker(name) for name in names}
        self.name = names[0]
        self.model = ""
        self.last_usage = (0, 0)

    def generate(self, system: str, user: str) -> Completion:
        def call(provider: Provider) -> Completion:
            completion = provider.generate(system, user)
            self.last_usage = provider.last_usage
            return completion

        return self._attempt("generate", call)

    def stream(self, system: str, user: str) -> Iterator[str]:
        def call(provider: Provider) -> Iterator[str]:
            chunks = provider.stream(system, user)
            first = next(chunks, None)
            return _resume(provider, chunks, first, self)

        return self._attempt("stream", call)

    def health(self) -> None:
        last: Optional[Exception] = None
        for name in self._names:
            try:
                self._provider(name).health()
                return
            except (ModelUnavailable, GenerationError) as exc:
                last = exc
        raise ModelUnavailable(
            f"No provider in the chain ({', '.join(self._names)}) is usable: {last}"
        )

    def _attempt(self, op: str, call: Callable[[Provider], _T]) -> _T:
        failures: list[str] = []
        previous = ""
        for name in self._names:
            breaker = self._breakers[name]
            if not breaker.allows():
                failures.append(f"{name}: breaker open")
                continue
            if previous:
                metrics.LLM_FAILOVER.labels(previous, name).inc()
            previous = name
            try:
                provider = self._provider(name)
                result = call(provider)
            except (ModelUnavailable, GenerationError) as exc:
                breaker.failed()
                failures.append(f"{name}: {exc}")
                logger.warning("Provider failed", extra={"provider": name, "op": op, "error": str(exc)})
                continue
            breaker.succeeded()
            self.name = name
            self.model = provider.model
            return result
        raise ModelUnavailable("Every generation provider failed. " + "; ".join(failures))

    def _provider(self, name: str) -> Provider:
        if name not in self._built:
            self._built[name] = _PROVIDERS[name]()
        return self._built[name]


def _resume(
    provider: Provider, chunks: Iterator[str], first: Optional[str], parent: FailoverProvider
) -> Iterator[str]:
    if first is not None:
        yield first
        yield from chunks
    parent.last_usage = provider.last_usage


def build_client() -> Provider:
    names = chain()
    unknown = [name for name in names if name not in _PROVIDERS]
    if unknown:
        raise ModelUnavailable(
            f"Unknown generation provider {', '.join(unknown)}. Choose from: {', '.join(_PROVIDERS)}."
        )
    if len(names) == 1:
        return _PROVIDERS[names[0]]()
    return FailoverProvider(names)


def chain() -> list[str]:
    """LLM_PROVIDER first, then LLM_FALLBACK_CHAIN, duplicates dropped."""
    names = [SETTINGS.models.llm_provider, *SETTINGS.models.llm_fallback_chain]
    return list(dict.fromkeys(names))


def _messages(system: str, user: str) -> list[Any]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
