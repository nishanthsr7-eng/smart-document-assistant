import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import _lru_cache_wrapper
from typing import Any, Callable

from sqlalchemy import text

from src.api import deps
from src.core.config import SETTINGS
from src.retrieval import tei
from src.storage import objects
from src.storage.db import engine
from src.storage.redis_client import client

_PROBE_TIMEOUT_S = 5.0
_MEMO_TTL_S = 10.0

_lock = threading.Lock()
_memo: tuple[float, dict[str, str]] = (0.0, {})


def check_health() -> dict[str, str]:
    """Readiness for a k8s probe: cached handles and cheap backend pings only.

    Never runs an embedding pass, never calls the generation provider, never scans a collection.
    The result is memoized so a tight probe interval cannot amplify into backend load.
    """
    with _lock:
        expires, cached = _memo
        if time.monotonic() < expires:
            return cached

    backends = {
        "postgres": _probe(_postgres),
        "redis": _probe(_redis),
        "object_store": _probe(objects.health),
    }
    # A TEI service is a remote dependency, so unlike lazily loaded local weights it does gate
    # readiness: with it down the replica cannot embed or rerank anything.
    for name, url in (("tei_embed", SETTINGS.models.tei_embed_url), ("tei_rerank", SETTINGS.models.tei_rerank_url)):
        if url:
            backends[name] = _probe(lambda u=url: tei.health(u))  # type: ignore[misc]
    # Model handles are reported but do not gate readiness: they load lazily on first use, and a
    # worker that has not served a request yet is still able to.
    checks = {
        **backends,
        "embedder": "tei" if SETTINGS.models.tei_embed_url else _loaded(deps.embedder),
        "reranker": "tei" if SETTINGS.models.tei_rerank_url else _loaded(deps.reranker),
        "llm": _loaded(deps.llm_client),
        "status": "ok" if all(v == "ok" for v in backends.values()) else "degraded",
        "provider": SETTINGS.models.llm_provider,
    }

    with _lock:
        globals()["_memo"] = (time.monotonic() + _MEMO_TTL_S, checks)
    return checks


def _probe(fn: Callable[[], None]) -> str:
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pool.submit(fn).result(timeout=_PROBE_TIMEOUT_S)
            return "ok"
        except FutureTimeout:
            return f"error: timed out after {_PROBE_TIMEOUT_S}s"
        except Exception as exc:
            return f"error: {exc}"


def _loaded(dep: "_lru_cache_wrapper[Any]") -> str:
    """Report whether a cached singleton is built, without building it."""
    return "loaded" if dep.cache_info().currsize else "not loaded"


def _postgres() -> None:
    with engine().connect() as conn:
        conn.execute(text("select 1"))


def _redis() -> None:
    client().ping()
