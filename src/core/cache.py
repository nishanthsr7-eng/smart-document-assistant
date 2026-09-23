import threading
import time
from collections import OrderedDict
from typing import Generic, Optional, TypeVar

from src.core.config import SETTINGS

V = TypeVar("V")


class TTLCache(Generic[V]):
    """Thread-safe LRU cache with per-entry time-to-live."""

    def __init__(self, max_size: int, ttl_s: float) -> None:
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._store: OrderedDict[str, tuple[float, V]] = OrderedDict()

    def get(self, key: str) -> Optional[V]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires, value = entry
            if time.monotonic() >= expires:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: V) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl_s, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# Shared across the answer pipeline; ingestion invalidates it when the corpus changes.
ANSWER_CACHE: TTLCache = TTLCache(SETTINGS.generation.cache_size, SETTINGS.generation.cache_ttl_s)
