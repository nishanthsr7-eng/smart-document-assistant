import json
import time
from typing import Any, Optional, cast

from arq.connections import ArqRedis, RedisSettings, create_pool
from redis.exceptions import RedisError

from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.storage.redis_client import client

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

_pool: Optional[ArqRedis] = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(SETTINGS.storage.redis_url)


async def pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def job_id_for(doc_id: str) -> str:
    """Idempotency key: same bytes, same job. A re-upload while one is in flight is a no-op."""
    return f"ingest:{doc_id}"


def _key(job_id: str) -> str:
    return f"job:{job_id}"


def _claim_key(doc_id: str) -> str:
    return f"job:claim:{doc_id}"


def claim(doc_id: str, job_id: str) -> Optional[str]:
    """Take the in-flight slot for these bytes, or return the job id that already holds it."""
    conn = client()
    if conn.set(_claim_key(doc_id), job_id, nx=True, ex=SETTINGS.jobs.job_timeout_s):
        return None
    held = cast(Optional[bytes], conn.get(_claim_key(doc_id)))
    return held.decode() if held is not None else None


def release(doc_id: str) -> None:
    client().delete(_claim_key(doc_id))


def _write(job_id: str, /, **fields: Any) -> None:
    conn = client()
    key = _key(job_id)
    payload = {k: v for k, v in fields.items() if v is not None}
    payload["updated_at"] = str(time.time())
    conn.hset(key, mapping=payload)
    conn.expire(key, SETTINGS.jobs.state_ttl_s)


def record_queued(job_id: str, doc_id: str, filename: str, owner: Principal) -> None:
    client().delete(_key(job_id))  # a re-upload reuses the key; the old attempt's report must go
    _write(
        job_id,
        job_id=job_id,
        doc_id=doc_id,
        filename=filename,
        # Stored on the job so job status and progress can be tenant-checked without
        # a document row: a queued job has none yet.
        tenant_id=owner.tenant_id,
        owner_id=owner.user_id,
        status=QUEUED,
        stage="Queued",
    )


def record_stage(job_id: str, stage: str) -> None:
    _write(job_id, status=RUNNING, stage=stage)


def record_done(job_id: str, doc_id: str, report: dict) -> None:
    _write(job_id, status=DONE, stage="Indexed", report=json.dumps(report))
    release(doc_id)


def record_failed(
    job_id: str, doc_id: str, filename: str, error: str, tenant_id: str
) -> None:
    _write(job_id, status=FAILED, stage="Failed", error=error)
    release(doc_id)
    conn = client()
    conn.lpush(
        SETTINGS.jobs.dlq_key,
        json.dumps(
            {
                "job_id": job_id,
                "doc_id": doc_id,
                "filename": filename,
                "tenant_id": tenant_id,
                "error": error,
                "failed_at": time.time(),
            }
        ),
    )
    conn.ltrim(SETTINGS.jobs.dlq_key, 0, SETTINGS.jobs.dlq_max_len - 1)


def get(job_id: str, tenant_id: Optional[str] = None) -> Optional[dict]:
    """Read job state. With a tenant_id, another tenant's job reads as if it did not exist."""
    raw = cast(dict[bytes, bytes], client().hgetall(_key(job_id)))
    if not raw:
        return None
    state = {k.decode(): v.decode() for k, v in raw.items()}
    if tenant_id is not None and state.get("tenant_id") != tenant_id:
        return None
    if "report" in state:
        state["report"] = json.loads(state["report"])
    return state


def dead_letters(tenant_id: str, limit: int = 50) -> list[dict]:
    """Poison documents for one tenant. The list is shared, so it is filtered on read."""
    items = cast(list[bytes], client().lrange(SETTINGS.jobs.dlq_key, 0, -1))
    entries = [json.loads(item) for item in items]
    return [e for e in entries if e.get("tenant_id") == tenant_id][:limit]


def queue_depth() -> Optional[int]:
    """Jobs waiting to be picked up. None when Redis is unreachable: a scrape must still return
    the rest of the metrics, and an unreachable Redis is what /health is for."""
    try:
        return int(cast(int, client().zcard(SETTINGS.jobs.queue_name)))
    except RedisError:
        return None
