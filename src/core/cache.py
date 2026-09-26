import pickle
from typing import Generic, Optional, TypeVar, cast

from src.core.config import SETTINGS
from src.storage.redis_client import client

V = TypeVar("V")


class RedisCache(Generic[V]):
    """Shared TTL cache. Every replica sees the same entries and the same invalidations."""

    def __init__(self, namespace: str, ttl_s: Optional[int]) -> None:
        self._prefix = f"cache:{namespace}:"
        self._ttl_s = ttl_s

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Optional[V]:
        raw = client().get(self._key(key))
        return cast(V, pickle.loads(cast(bytes, raw))) if raw is not None else None

    def set(self, key: str, value: V) -> None:
        payload = pickle.dumps(value)
        if self._ttl_s is None:
            client().set(self._key(key), payload)
        else:
            client().set(self._key(key), payload, ex=self._ttl_s)

    def delete(self, key: str) -> None:
        client().delete(self._key(key))

    def clear(self) -> None:
        conn = client()
        for batch in _scan(conn, f"{self._prefix}*"):
            conn.delete(*batch)


def _scan(conn, pattern: str, batch_size: int = 500):
    cursor = 0
    while True:
        cursor, keys = conn.scan(cursor=cursor, match=pattern, count=batch_size)
        if keys:
            yield keys
        if cursor == 0:
            return


ANSWER_CACHE: RedisCache = RedisCache("answers", SETTINGS.generation.cache_ttl_s)
# Parent payloads are immutable for a given doc_id (content hash); invalidated on delete only.
DOC_CACHE: RedisCache[dict] = RedisCache("parents", ttl_s=None)
