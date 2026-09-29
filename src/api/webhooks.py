"""Ingest-completion callbacks: registration, signing, and delivery.

A webhook URL is caller-controlled input that this service then fetches with its own network
identity, so registration validates the target rather than trusting it, and delivery is signed
so a receiver can tell our POST from anyone else's.
"""

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Union
from urllib.parse import urlparse

import anyio
import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update

from src.core import logs, metrics
from src.core.config import SETTINGS
from src.core.errors import DocumentNotFound, PermissionDenied
from src.storage.db import session
from src.storage.models import Webhook

logger = logs.logger(__name__)

SIGNATURE_HEADER = "X-SDA-Signature"
TIMESTAMP_HEADER = "X-SDA-Timestamp"
EVENT_HEADER = "X-SDA-Event"
DELIVERY_HEADER = "X-SDA-Delivery"

INGEST_COMPLETED = "ingest.completed"
INGEST_FAILED = "ingest.failed"


def register(tenant_id: str, url: str, events: list[str]) -> dict:
    """Returns the secret exactly once. It is never readable again -- a receiver that loses it
    rotates the subscription rather than reading it back out of this API."""
    _validate_target(url)
    subscribed = events or list(SETTINGS.webhooks.events)
    unknown = set(subscribed) - set(SETTINGS.webhooks.events)
    if unknown:
        raise PermissionDenied(f"Unknown webhook events: {sorted(unknown)}.")
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    with session() as sess:
        existing = len(list(sess.scalars(
            select(Webhook.webhook_id).where(Webhook.tenant_id == tenant_id)
        )))
        if existing >= SETTINGS.webhooks.max_per_tenant:
            raise PermissionDenied(
                f"A tenant may register at most {SETTINGS.webhooks.max_per_tenant} webhooks."
            )
        hook = Webhook(
            webhook_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            url=url,
            secret=secret,
            events=subscribed,
            active=True,
            consecutive_failures=0,
        )
        sess.add(hook)
        sess.flush()
        created = _as_dict(hook)
    return {**created, "secret": secret}


def listing(tenant_id: str) -> list[dict]:
    with session() as sess:
        stmt = select(Webhook).where(Webhook.tenant_id == tenant_id).order_by(Webhook.created_at)
        return [_as_dict(hook) for hook in sess.scalars(stmt)]


def unregister(tenant_id: str, webhook_id: str) -> None:
    with session() as sess:
        hook = sess.scalars(
            select(Webhook).where(
                Webhook.webhook_id == webhook_id, Webhook.tenant_id == tenant_id
            )
        ).first()
        if hook is None:
            raise DocumentNotFound(webhook_id)
        sess.execute(sa_delete(Webhook).where(Webhook.webhook_id == webhook_id))


def _as_dict(hook: Webhook) -> dict:
    return {
        "webhook_id": hook.webhook_id,
        "url": hook.url,
        "events": list(hook.events),
        "active": hook.active,
        "consecutive_failures": hook.consecutive_failures,
        "last_error": hook.last_error,
        "last_delivery_at": hook.last_delivery_at.isoformat() if hook.last_delivery_at else None,
        "created_at": hook.created_at.isoformat() if hook.created_at else None,
    }


_Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def _validate_target(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PermissionDenied("A webhook URL must be an absolute http(s) URL.")
    if SETTINGS.webhooks.allow_private_targets:
        return
    if parsed.scheme != "https":
        raise PermissionDenied("A webhook URL must use https.")
    # Checked here and again at delivery: this catches the obvious case, and the delivery-time
    # check is what a DNS record flipped after registration runs into.
    for address in _resolve(parsed.hostname):
        if not address.is_global:
            raise PermissionDenied("A webhook URL must resolve to a public address.")


def _resolve(hostname: str) -> list[_Address]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise PermissionDenied(f"Could not resolve webhook host '{hostname}'.") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def sign(secret: str, timestamp: str, payload: bytes) -> str:
    """The timestamp is inside the MAC, so a captured delivery cannot be replayed under a new
    one, and a receiver can reject anything older than its own tolerance."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


async def dispatch(tenant_id: str, event: str, data: dict[str, Any]) -> None:
    """Deliver one event to every active subscription. Never raises: a receiver being down is
    the receiver's problem, and it must not turn a finished ingest into a failed job."""
    if not SETTINGS.webhooks.enabled:
        return
    hooks = await anyio.to_thread.run_sync(_subscribers, tenant_id, event)
    if not hooks:
        return
    delivery_id = uuid.uuid4().hex
    timestamp = str(int(time.time()))
    payload = json.dumps(
        {"id": delivery_id, "event": event, "created_at": timestamp, "data": data}
    ).encode()
    async with httpx.AsyncClient(timeout=SETTINGS.webhooks.timeout_s) as http:
        for hook in hooks:
            await _deliver(http, hook, event, delivery_id, timestamp, payload)


def _subscribers(tenant_id: str, event: str) -> list[dict]:
    with session() as sess:
        stmt = select(Webhook).where(Webhook.tenant_id == tenant_id, Webhook.active.is_(True))
        return [
            {"webhook_id": h.webhook_id, "url": h.url, "secret": h.secret}
            for h in sess.scalars(stmt)
            if event in h.events
        ]


async def _deliver(
    http: httpx.AsyncClient,
    hook: dict,
    event: str,
    delivery_id: str,
    timestamp: str,
    payload: bytes,
) -> None:
    headers = {
        "Content-Type": "application/json",
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(hook["secret"], timestamp, payload),
    }
    error: Optional[str] = None
    for attempt in range(SETTINGS.webhooks.max_attempts):
        try:
            _validate_target(hook["url"])
            response = await http.post(hook["url"], content=payload, headers=headers)
            if response.status_code < 400:
                await anyio.to_thread.run_sync(_record, hook["webhook_id"], None)
                metrics.WEBHOOK_DELIVERIES.labels(event, "delivered").inc()
                return
            error = f"HTTP {response.status_code}"
        except (httpx.HTTPError, PermissionDenied) as exc:
            error = str(exc) or type(exc).__name__
        if attempt + 1 < SETTINGS.webhooks.max_attempts:
            await anyio.sleep(SETTINGS.webhooks.backoff_s * (2**attempt))
    await anyio.to_thread.run_sync(_record, hook["webhook_id"], error or "delivery failed")
    metrics.WEBHOOK_DELIVERIES.labels(event, "failed").inc()
    logger.warning(
        "Webhook delivery failed", extra={"webhook_id": hook["webhook_id"], "error": error}
    )


def _record(webhook_id: str, error: Optional[str]) -> None:
    """A run of failures disables the subscription; one success clears the count."""
    now = datetime.now(timezone.utc)
    with session() as sess:
        if error is None:
            sess.execute(
                update(Webhook)
                .where(Webhook.webhook_id == webhook_id)
                .values(consecutive_failures=0, last_error=None, last_delivery_at=now)
            )
            return
        hook = sess.scalars(select(Webhook).where(Webhook.webhook_id == webhook_id)).first()
        if hook is None:
            return
        failures = hook.consecutive_failures + 1
        sess.execute(
            update(Webhook)
            .where(Webhook.webhook_id == webhook_id)
            .values(
                consecutive_failures=failures,
                last_error=error[:512],
                last_delivery_at=now,
                active=failures < SETTINGS.webhooks.max_failures,
            )
        )
