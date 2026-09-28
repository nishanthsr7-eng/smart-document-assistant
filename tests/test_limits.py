import time

import pytest
from fastapi.testclient import TestClient

from src.api import deps, router
from src.auth.principal import Principal
from src.core import limits
from src.core.config import SETTINGS, replace
from src.core.errors import RateLimited
from src.generation.client import NullProvider

TENANT = "00000000-0000-0000-0000-0000000000cc"


@pytest.fixture
def buckets(storage_stack):
    """Redis is required: the limiter has no in-process fallback, by design."""
    yield


def _limits(monkeypatch, **overrides):
    monkeypatch.setattr(
        limits, "SETTINGS", replace(SETTINGS, limits=replace(SETTINGS.limits, **overrides))
    )


def test_bucket_allows_the_burst_then_refuses(buckets, monkeypatch):
    _limits(monkeypatch, query_per_minute=60, query_burst=3)
    for _ in range(3):
        limits.check_rate("query", TENANT)
    with pytest.raises(RateLimited) as exc:
        limits.check_rate("query", TENANT)
    assert exc.value.scope == "query"
    assert exc.value.retry_after_s >= 1


def test_buckets_are_per_subject_and_per_scope(buckets, monkeypatch):
    _limits(monkeypatch, query_per_minute=60, query_burst=1, ingest_per_minute=60, ingest_burst=1)
    limits.check_rate("query", TENANT)
    # Another tenant and another endpoint each start full.
    limits.check_rate("query", "other-tenant")
    limits.check_rate("ingest", TENANT)
    with pytest.raises(RateLimited):
        limits.check_rate("query", TENANT)


def test_bucket_refills_over_time(buckets, monkeypatch):
    _limits(monkeypatch, query_per_minute=600, query_burst=1)
    limits.check_rate("query", TENANT)
    with pytest.raises(RateLimited):
        limits.check_rate("query", TENANT)
    # 600/min is one token every 100ms; the bucket is written with a wall-clock stamp, so the
    # refill is real elapsed time rather than a counter reset.
    time.sleep(0.25)
    limits.check_rate("query", TENANT)


def test_disabled_limiter_never_refuses(buckets, monkeypatch):
    _limits(monkeypatch, enabled=False, query_per_minute=60, query_burst=1)
    for _ in range(5):
        limits.check_rate("query", TENANT)


def test_spend_accumulates_and_exhausts_the_token_budget(buckets, monkeypatch):
    _limits(monkeypatch, daily_tokens=100, daily_cost_usd=0)
    limits.check_budget(TENANT)
    limits.record_spend(TENANT, 60, 0.01)
    limits.check_budget(TENANT)
    limits.record_spend(TENANT, 60, 0.01)
    assert limits.usage(TENANT) == (120, pytest.approx(0.02))
    with pytest.raises(RateLimited) as exc:
        limits.check_budget(TENANT)
    assert exc.value.scope == "budget"


def test_cost_budget_is_enforced_independently(buckets, monkeypatch):
    _limits(monkeypatch, daily_tokens=0, daily_cost_usd=0.05)
    limits.record_spend(TENANT, 10, 0.06)
    with pytest.raises(RateLimited) as exc:
        limits.check_budget(TENANT)
    assert "0.06" in exc.value.message


def test_no_budget_configured_means_no_budget_check(buckets, monkeypatch):
    _limits(monkeypatch, daily_tokens=0, daily_cost_usd=0)
    limits.record_spend(TENANT, 10_000_000, 999.0)
    limits.check_budget(TENANT)


EDITOR = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    tenant_id=TENANT,
    email="editor@acme.test",
    role="editor",
)


class _FakeVectorStore:
    def query(self, vector, k, doc_ids):
        return []


class _FakeEmbedder:
    def encode(self, texts):
        return [[0.0] for _ in texts]

    def encode_query(self, texts):
        return [[0.0] for _ in texts]


@pytest.fixture
def api(buckets, monkeypatch):
    """The limiter runs before retrieval and generation, so both are stubbed out: what is under
    test is which requests reach the pipeline at all."""
    monkeypatch.setattr(deps, "vector_store", lambda: _FakeVectorStore())
    monkeypatch.setattr(deps, "embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(deps, "llm_client", lambda: NullProvider())
    router.app.dependency_overrides[deps.current_principal] = lambda: EDITOR
    yield TestClient(router.app)
    router.app.dependency_overrides.clear()


def test_query_over_the_rate_limit_answers_429_with_retry_after(api, monkeypatch):
    # One per minute: the bucket must not have refilled by the time the second call lands.
    _limits(monkeypatch, query_per_minute=1, query_burst=1)
    body = {"question": "What is the leave policy?", "mode": "hybrid_rerank", "generate": False}

    first = api.post("/query", json=body)
    second = api.post("/query", json=body)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"].isdigit()
    assert second.json()["scope"] == "query"


def test_ingest_over_the_rate_limit_answers_429(api, monkeypatch):
    _limits(monkeypatch, ingest_per_minute=60, ingest_burst=0)
    response = api.post("/ingest", files={"file": ("policy.txt", b"hello", "text/plain")})
    assert response.status_code == 429
    assert response.json()["scope"] == "ingest"


def test_exhausted_budget_refuses_generation_but_not_retrieval(api, monkeypatch):
    _limits(monkeypatch, daily_tokens=10, daily_cost_usd=0)
    limits.record_spend(TENANT, 50, 0.01)

    refused = api.post("/query", json={"question": "What is the leave policy?", "generate": True})
    assert refused.status_code == 429
    assert refused.json()["scope"] == "budget"

    # Retrieval costs nothing to generate, so a spent tenant can still search its documents.
    allowed = api.post("/query", json={"question": "What is the leave policy?", "generate": False})
    assert allowed.status_code == 200
