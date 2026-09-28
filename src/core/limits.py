"""Per-tenant rate limits and daily spend budgets, held in Redis.

Two independent controls, because they fail for different reasons. A *rate limit* is a token
bucket per tenant and endpoint: it bounds how fast the service is asked to work, and refills on
its own. A *budget* is a daily counter of generation tokens and USD: it bounds what a tenant can
spend, and only a new day (or a config change) clears it.

Both are shared state, so four API workers and the ingest worker enforce one limit rather than
four. The bucket is a Lua script so read-refill-write is atomic under concurrency.
"""

import math
import time
from datetime import datetime, timezone
from typing import Any, cast

from src.core import metrics
from src.core.config import SETTINGS
from src.core.errors import RateLimited
from src.storage.redis_client import client

# Refill, spend, persist, expire -- one round trip and no lost update between the read and the
# write. KEYS[1] is the bucket; ARGV is rate/s, burst, now, cost, ttl.
_BUCKET = """
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local rate, burst, now, cost, ttl = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], ttl)
return {allowed, tostring(cost - tokens)}
"""


def check_rate(scope: str, subject: str) -> None:
    """Take one token from `subject`'s bucket in `scope` ("query", "ingest" or "auth"), or raise."""
    cfg = SETTINGS.limits
    if not cfg.enabled:
        return
    per_minute, burst = getattr(cfg, f"{scope}_per_minute"), getattr(cfg, f"{scope}_burst")
    if per_minute <= 0:
        return
    rate = per_minute / 60.0
    allowed, deficit = cast(
        list[Any],
        client().eval(
            _BUCKET,
            1,
            f"ratelimit:{scope}:{subject}",
            str(rate),
            str(burst),
            str(time.time()),
            "1",
            "3600",
        ),
    )
    if allowed:
        return
    retry_after = max(1, math.ceil(float(deficit) / rate))
    metrics.RATE_LIMITED.labels(scope, "rate").inc()
    raise RateLimited(
        f"Rate limit exceeded: at most {per_minute:g} {scope} requests per minute.",
        retry_after,
        scope,
    )


def check_budget(tenant_id: str) -> None:
    """Raise RateLimited if the tenant has spent its day's tokens or dollars.

    Checked before generation, metered after it, so the request that crosses a budget is served
    and the next one is refused. Billing a caller for work it was not allowed to start would be
    the alternative, and a partial overshoot of one answer is the cheaper error.
    """
    cfg = SETTINGS.limits
    if not cfg.enabled or not (cfg.daily_tokens or cfg.daily_cost_usd):
        return
    tokens, cost = usage(tenant_id)
    if cfg.daily_tokens and tokens >= cfg.daily_tokens:
        _over_budget("tokens", f"{tokens} of {cfg.daily_tokens} tokens")
    if cfg.daily_cost_usd and cost >= cfg.daily_cost_usd:
        _over_budget("cost", f"${cost:.2f} of ${cfg.daily_cost_usd:.2f}")


def record_spend(tenant_id: str, tokens: int, cost_usd: float) -> None:
    """Meter one generation against the tenant's day. Called from the answerer's Generating
    stage, which is the only place that knows both the usage and whose query it was."""
    if not SETTINGS.limits.enabled or not tenant_id or tokens <= 0:
        return
    key = _budget_key(tenant_id)
    pipe = client().pipeline()
    pipe.hincrby(key, "tokens", tokens)
    pipe.hincrbyfloat(key, "cost_usd", cost_usd)
    pipe.expire(key, SETTINGS.limits.budget_ttl_s)
    pipe.execute()


def usage(tenant_id: str) -> tuple[int, float]:
    """Today's (tokens, usd) for a tenant, UTC days."""
    raw = cast(list[Any], client().hmget(_budget_key(tenant_id), ["tokens", "cost_usd"]))
    return int(raw[0] or 0), float(raw[1] or 0.0)


def _budget_key(tenant_id: str) -> str:
    return f"budget:{tenant_id}:{datetime.now(timezone.utc):%Y-%m-%d}"


def _over_budget(reason: str, spent: str) -> None:
    metrics.RATE_LIMITED.labels("budget", reason).inc()
    raise RateLimited(
        f"Daily budget exhausted for this tenant ({spent}). It resets at 00:00 UTC.",
        _seconds_to_utc_midnight(),
        "budget",
    )


def _seconds_to_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    return max(1, 86400 - (now.hour * 3600 + now.minute * 60 + now.second))
