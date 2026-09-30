import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import _lru_cache_wrapper
from typing import Any, Callable

from sqlalchemy import text

from src.api import deps
from src.core.config import SETTINGS
from src.generation import client as llm
from src.retrieval import tei
from src.storage import objects
from src.storage.db import engine
from src.storage.redis_client import client

_PROBE_TIMEOUT_S = 5.0
_MEMO_TTL_S = 10.0

# Reported while draining: the backends are almost certainly fine, and saying so would invite a
# balancer to keep sending work to a process that is on its way out.
_UNKNOWN = {
    "postgres": "draining",
    "redis": "draining",
    "object_store": "draining",
    "embedder": "draining",
    "reranker": "draining",
    "llm": "draining",
}

_lock = threading.Lock()
_memo: tuple[float, dict[str, str]] = (0.0, {})
_draining = threading.Event()


def start_draining() -> None:
    """Fail readiness from now on, while still serving what is in flight.

    Shutdown order matters for a streaming API. A replica that stops accepting connections
    while the load balancer still believes it is healthy drops the answers already streaming
    through it; one that reports unready first is taken out of rotation, finishes them, and
    then exits. This is the flag that separates the two, and `DRAIN_DELAY_S` is how long the
    process then waits for the balancer to notice.
    """
    _draining.set()
    with _lock:
        globals()["_memo"] = (0.0, {})


def draining() -> bool:
    return _draining.is_set()


def check_health() -> dict[str, str]:
    """Readiness for a k8s probe: cached handles and cheap backend pings only.

    Never runs an embedding pass, never calls the generation provider, never scans a collection.
    The result is memoized so a tight probe interval cannot amplify into backend load.
    """
    if _draining.is_set():
        return {**_UNKNOWN, "status": "draining", "provider": ",".join(llm.chain())}
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
        # The whole chain, in order: which one is serving is a per-request outcome, and
        # sda_llm_breaker_open is where an outage shows up.
        "provider": ",".join(llm.chain()),
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
