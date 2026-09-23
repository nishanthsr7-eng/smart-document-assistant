import random
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, TypeVar

import httpx

from src.core.config import SETTINGS
from src.core.errors import GenerationError, ModelUnavailable

_T = TypeVar("_T")
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4


def _retry_transient(call: Callable[[], _T], status_of: Callable[[Exception], int | None]) -> _T:
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return call()
        except Exception as exc:
            status = status_of(exc)
            if status not in _TRANSIENT_STATUS or attempt == _MAX_RETRIES:
                raise
            time.sleep(2**attempt + random.uniform(0, 1))


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int


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
        return Completion(
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
        return Completion(
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
        self._client = Groq(api_key=key)
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
        return Completion(
            res.choices[0].message.content or "",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )

    def health(self) -> None:
        if not SETTINGS.models.groq_api_key:
            raise ModelUnavailable("GROQ_API_KEY is not set.")


_PROVIDERS = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
}


def build_client() -> Provider:
    name = SETTINGS.models.llm_provider
    if name not in _PROVIDERS:
        raise ModelUnavailable(
            f"Unknown LLM_PROVIDER '{name}'. Choose one of: {', '.join(_PROVIDERS)}."
        )
    return _PROVIDERS[name]()


def _messages(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
