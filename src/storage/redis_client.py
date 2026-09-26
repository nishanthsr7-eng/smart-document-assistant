import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

import redis

from src.core.config import SETTINGS
from src.core.errors import StorageError

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@lru_cache(maxsize=1)
def client() -> redis.Redis:
    return redis.Redis.from_url(SETTINGS.storage.redis_url)


@contextmanager
def lock(name: str, ttl_s: int, wait_s: float = 30.0) -> Iterator[None]:
    """Blocking cross-process lock. Fails loudly rather than proceeding unserialized."""
    token = str(uuid.uuid4())
    key = f"lock:{name}"
    conn = client()
    acquired = conn.set(key, token, nx=True, px=int(ttl_s * 1000))
    if not acquired:
        acquired = _wait_for(conn, key, token, ttl_s, wait_s)
    if not acquired:
        raise StorageError(f"Could not acquire lock '{name}' within {wait_s}s.")
    try:
        yield
    finally:
        conn.eval(_RELEASE, 1, key, token)


def _wait_for(conn: redis.Redis, key: str, token: str, ttl_s: int, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if conn.set(key, token, nx=True, px=int(ttl_s * 1000)):
            return True
    return False
