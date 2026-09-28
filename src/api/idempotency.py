"""`Idempotency-Key` for unsafe requests, backed by Redis so every replica shares one record.

Three outcomes, and the distinction between them is the whole point: a replay of a finished
request replays its response, a replay while the first one is still in flight is a 409 rather
than a second execution, and the same key with a different body is a client bug worth a 422
instead of the first response silently standing in for the second.

`/ingest` already collapses identical bytes into one job by content hash. This is the other
half: two *different* files sent under one key, which content hashing cannot see.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, cast

from fastapi import HTTPException

from src.core.config import SETTINGS
from src.storage.redis_client import client

_IN_FLIGHT = "in-flight"


@dataclass(frozen=True)
class Replay:
    status: int
    body: dict[str, Any]


def fingerprint(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


def _key(tenant_id: str, endpoint: str, key: str) -> str:
    return f"idem:{tenant_id}:{endpoint}:{key}"


def begin(tenant_id: str, endpoint: str, key: str, body_fingerprint: str) -> Optional[Replay]:
    """Claim the key, or describe what already holds it. None means the caller owns it now."""
    redis_key = _key(tenant_id, endpoint, key)
    claim = json.dumps({"state": _IN_FLIGHT, "fingerprint": body_fingerprint})
    conn = client()
    if conn.set(redis_key, claim, nx=True, ex=SETTINGS.api.idempotency_ttl_s):
        return None

    raw = cast(Optional[bytes], conn.get(redis_key))
    if raw is None:
        # Expired between the SET and the GET. Retrying the claim would race again; executing
        # is the safe answer, because the record it would have replayed is gone anyway.
        return None
    record = json.loads(raw)
    if record["fingerprint"] != body_fingerprint:
        raise HTTPException(
            status_code=422,
            detail="This Idempotency-Key was already used with a different request body.",
        )
    if record["state"] == _IN_FLIGHT:
        raise HTTPException(
            status_code=409,
            detail="A request with this Idempotency-Key is still in flight.",
            headers={"Retry-After": "1"},
        )
    return Replay(status=record["status"], body=record["body"])


def complete(
    tenant_id: str, endpoint: str, key: str, body_fingerprint: str, status: int, body: dict
) -> None:
    client().set(
        _key(tenant_id, endpoint, key),
        json.dumps(
            {"state": "done", "fingerprint": body_fingerprint, "status": status, "body": body}
        ),
        ex=SETTINGS.api.idempotency_ttl_s,
    )


def abandon(tenant_id: str, endpoint: str, key: str) -> None:
    """Drop the claim of a request that failed, so the client can retry the same key."""
    client().delete(_key(tenant_id, endpoint, key))
