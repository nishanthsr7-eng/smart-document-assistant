"""Item 20: the API's own contract -- versioning, pagination, problem+json, idempotency,
conditional reads, bulk ingest, the non-streaming answer, and webhook signing.

These assert the shape a consumer depends on, not the behaviour behind it: a change that makes
`/documents` return a bare list again, or an error return `{"detail": ...}` alone, is a breaking
change for every client and should fail here rather than in someone else's integration.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import conditional, idempotency, pagination, problems, webhooks
from src.api.router import app
from src.auth.principal import Principal
from src.core.config import SETTINGS, replace
from src.core.errors import DocumentNotFound, PermissionDenied

ADMIN = Principal(
    user_id="00000000-0000-0000-0000-000000000009",
    tenant_id="00000000-0000-0000-0000-0000000000aa",
    email="admin@acme.test",
    role="admin",
)


@pytest.fixture
def admin_client():
    from src.api import deps

    app.dependency_overrides[deps.current_principal] = lambda: ADMIN
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _document(index: int, created: datetime) -> dict:
    return {
        "doc_id": f"d{index}",
        "filename": f"doc{index}.pdf",
        "pages": 1,
        "num_children": 1,
        "owner_id": ADMIN.user_id,
        "created_at": created,
    }


# --- versioning ---


def test_every_route_is_served_under_v1_and_unversioned(admin_client):
    """The unversioned mount is the compatibility path, so both must reach the same handler."""
    assert admin_client.get("/v1/config").json() == admin_client.get("/config").json()


def test_openapi_describes_one_api_not_two():
    paths = app.openapi()["paths"]
    assert "/v1/documents" in paths
    assert "/documents" not in paths


def test_probes_are_not_versioned(admin_client):
    """A load balancer's health check must not have to know which contract is deployed."""
    assert admin_client.get("/livez").status_code == 200
    assert admin_client.get("/v1/livez").status_code == 404


# --- RFC 9457 problem+json ---


def test_errors_are_problem_json(admin_client):
    response = admin_client.get("/v1/jobs/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(problems.CONTENT_TYPE)
    body = response.json()
    assert body["type"].startswith(problems.BASE_TYPE)
    assert body == {
        **body,
        "title": "Not found",
        "status": 404,
        "detail": "Unknown job.",
        "instance": "/v1/jobs/nope",
    }


def test_problem_keeps_detail_because_every_client_reads_it(admin_client):
    response = admin_client.post("/v1/query", json={"question": "hi", "mode": "nonsense"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown mode 'nonsense'."


def test_validation_errors_carry_the_per_field_report(admin_client):
    response = admin_client.post("/v1/query", json={"doc_ids": []})
    assert response.status_code == 422
    body = response.json()
    assert body["type"].endswith("/unprocessable-content")
    assert any(error["field"] == "question" for error in body["errors"])


# --- pagination ---


def test_documents_pages_and_issues_a_cursor(admin_client, monkeypatch):
    from src.api import router

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [_document(i, base + timedelta(minutes=i)) for i in range(5)]

    def fake_list(tenant_id, limit=None, after=None):
        remaining = [r for r in rows if after is None or (r["created_at"], r["doc_id"]) > after]
        return remaining[:limit] if limit else remaining

    monkeypatch.setattr(router, "list_indexed", fake_list)

    first = admin_client.get("/v1/documents?limit=2").json()
    assert [item["doc_id"] for item in first["items"]] == ["d0", "d1"]
    assert first["next_cursor"]

    second = admin_client.get(f"/v1/documents?limit=2&cursor={first['next_cursor']}").json()
    assert [item["doc_id"] for item in second["items"]] == ["d2", "d3"]

    last = admin_client.get(f"/v1/documents?limit=2&cursor={second['next_cursor']}").json()
    assert [item["doc_id"] for item in last["items"]] == ["d4"]
    # The extra-row probe is what makes this None rather than a cursor to an empty page.
    assert last["next_cursor"] is None


def test_page_size_is_capped_not_refused(admin_client, monkeypatch):
    from src.api import router

    seen = {}

    def fake_list(tenant_id, limit=None, after=None):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(router, "list_indexed", fake_list)
    assert admin_client.get("/v1/documents?limit=100000").status_code == 200
    # One over the cap, because the extra row is the has-more probe.
    assert seen["limit"] == SETTINGS.api.max_page_size + 1


def test_a_malformed_cursor_is_the_callers_error(admin_client):
    response = admin_client.get("/v1/documents?cursor=not-base64!!")
    assert response.status_code == 400
    assert response.json()["detail"] == "Malformed cursor."


def test_cursor_round_trips():
    stamp = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    assert pagination.decode(pagination.encode(stamp, "abc")) == (stamp, "abc")


# --- ETags ---


def test_unchanged_documents_read_as_304(admin_client, monkeypatch):
    from src.api import router

    rows = [_document(0, datetime(2026, 1, 1, tzinfo=timezone.utc))]
    monkeypatch.setattr(router, "list_indexed", lambda tenant_id, limit=None, after=None: rows)

    first = admin_client.get("/v1/documents")
    etag = first.headers["etag"]
    again = admin_client.get("/v1/documents", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


def test_etag_changes_with_the_body(admin_client, monkeypatch):
    from src.api import router

    rows = [_document(0, datetime(2026, 1, 1, tzinfo=timezone.utc))]
    monkeypatch.setattr(router, "list_indexed", lambda tenant_id, limit=None, after=None: rows)
    first = admin_client.get("/v1/documents").headers["etag"]
    rows.append(_document(1, datetime(2026, 1, 2, tzinfo=timezone.utc)))
    assert admin_client.get("/v1/documents").headers["etag"] != first


def test_if_none_match_star_matches_any_representation():
    assert conditional.matches(
        SimpleNamespace(headers={"if-none-match": "*"}), '"whatever"'
    )


# --- idempotency ---


def test_idempotency_replays_the_first_response(storage_stack):
    key = "idem-test-replay"
    idempotency.abandon(ADMIN.tenant_id, "ingest", key)
    assert idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp") is None
    idempotency.complete(ADMIN.tenant_id, "ingest", key, "fp", 202, {"job_id": "j1"})
    replay = idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp")
    assert replay is not None
    assert (replay.status, replay.body) == (202, {"job_id": "j1"})


def test_the_same_key_with_a_different_body_is_a_client_bug(storage_stack):
    key = "idem-test-mismatch"
    idempotency.abandon(ADMIN.tenant_id, "ingest", key)
    idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp-one")
    idempotency.complete(ADMIN.tenant_id, "ingest", key, "fp-one", 202, {})
    with pytest.raises(Exception) as raised:
        idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp-two")
    assert raised.value.status_code == 422


def test_a_replay_while_the_first_is_in_flight_is_a_conflict(storage_stack):
    key = "idem-test-inflight"
    idempotency.abandon(ADMIN.tenant_id, "ingest", key)
    assert idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp") is None
    with pytest.raises(Exception) as raised:
        idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp")
    assert raised.value.status_code == 409


def test_an_abandoned_key_can_be_retried(storage_stack):
    key = "idem-test-abandon"
    idempotency.abandon(ADMIN.tenant_id, "ingest", key)
    idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp")
    idempotency.abandon(ADMIN.tenant_id, "ingest", key)
    assert idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp") is None


def test_idempotency_is_scoped_to_one_tenant(storage_stack):
    key = "idem-test-tenant"
    other = "00000000-0000-0000-0000-0000000000bb"
    idempotency.abandon(ADMIN.tenant_id, "ingest", key)
    idempotency.abandon(other, "ingest", key)
    assert idempotency.begin(ADMIN.tenant_id, "ingest", key, "fp") is None
    assert idempotency.begin(other, "ingest", key, "fp") is None


# --- bulk ingest ---


def test_bulk_ingest_is_refused_past_the_cap(admin_client):
    files = [
        ("files", (f"f{i}.txt", b"hello", "text/plain"))
        for i in range(SETTINGS.api.max_bulk_files + 1)
    ]
    response = admin_client.post("/v1/ingest/batch", files=files)
    assert response.status_code == 422
    assert "per batch" in response.json()["detail"]


def test_bulk_ingest_reports_a_bad_file_without_failing_the_batch(admin_client, monkeypatch):
    from src.api import router

    async def fake_accept(file, principal, idempotency_key):
        if file.filename.endswith(".exe"):
            from src.core.errors import UnsupportedFormat

            raise UnsupportedFormat("exe")
        return 202, router.JobOut(
            job_id="j", doc_id="d", filename=file.filename, status="queued", stage="Queued"
        )

    monkeypatch.setattr(router, "_accept_upload", fake_accept)
    monkeypatch.setattr(router.limits, "check_rate", lambda *a, **k: None)

    response = admin_client.post(
        "/v1/ingest/batch",
        files=[
            ("files", ("good.txt", b"hello", "text/plain")),
            ("files", ("bad.exe", b"hello", "application/octet-stream")),
        ],
    )
    assert response.status_code == 202
    body = response.json()
    assert [job["filename"] for job in body["accepted"]] == ["good.txt"]
    assert body["rejected"] == [
        {"filename": "bad.exe", "detail": "'.exe' files aren't supported. Upload a PDF or TXT file."}
    ]


# --- webhook signing ---


def test_signature_covers_the_timestamp_so_a_delivery_cannot_be_replayed():
    payload = json.dumps({"event": "ingest.completed"}).encode()
    assert webhooks.sign("secret", "100", payload) != webhooks.sign("secret", "101", payload)


def test_signature_is_secret_specific():
    payload = b"{}"
    assert webhooks.sign("a", "1", payload) != webhooks.sign("b", "1", payload)


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/hook", "not-a-url", "http://example.com/hook"],
)
def test_a_webhook_target_must_be_an_absolute_https_url(url, monkeypatch):
    _public_only(monkeypatch)
    with pytest.raises(PermissionDenied):
        webhooks._validate_target(url)


def test_a_webhook_may_not_point_at_a_private_address(monkeypatch):
    """SSRF: without this a tenant has this service fetch the cluster's metadata endpoint with
    its own network identity."""
    import ipaddress

    _public_only(monkeypatch)
    monkeypatch.setattr(
        webhooks, "_resolve", lambda host: [ipaddress.ip_address("169.254.169.254")]
    )
    with pytest.raises(PermissionDenied):
        webhooks._validate_target("https://metadata.internal/hook")


def _public_only(monkeypatch) -> None:
    monkeypatch.setattr(
        webhooks,
        "SETTINGS",
        replace(SETTINGS, webhooks=replace(SETTINGS.webhooks, allow_private_targets=False)),
    )


# --- webhook registration, against the real database ---


def test_webhooks_are_scoped_to_the_registering_tenant(tenants, monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "SETTINGS",
        replace(SETTINGS, webhooks=replace(SETTINGS.webhooks, allow_private_targets=True)),
    )
    a = webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:9999/hook", [])
    webhooks.register(tenants.b.tenant_id, "http://127.0.0.1:9998/hook", [])

    assert [h["webhook_id"] for h in webhooks.listing(tenants.a.tenant_id)] == [a["webhook_id"]]
    # Another tenant's id reads as absent rather than forbidden: existence is tenant-scoped.
    with pytest.raises(DocumentNotFound):
        webhooks.unregister(tenants.b.tenant_id, a["webhook_id"])
    assert len(webhooks.listing(tenants.a.tenant_id)) == 1

    webhooks.unregister(tenants.a.tenant_id, a["webhook_id"])
    assert webhooks.listing(tenants.a.tenant_id) == []


def test_the_secret_is_returned_once_and_never_listed(tenants, monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "SETTINGS",
        replace(SETTINGS, webhooks=replace(SETTINGS.webhooks, allow_private_targets=True)),
    )
    created = webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:9999/hook", [])
    assert created["secret"]
    assert "secret" not in webhooks.listing(tenants.a.tenant_id)[0]


def test_a_tenant_cannot_register_more_than_the_cap(tenants, monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "SETTINGS",
        replace(
            SETTINGS,
            webhooks=replace(SETTINGS.webhooks, allow_private_targets=True, max_per_tenant=2),
        ),
    )
    webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:1/hook", [])
    webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:2/hook", [])
    with pytest.raises(PermissionDenied):
        webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:3/hook", [])


def test_an_unknown_event_name_is_refused(tenants, monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "SETTINGS",
        replace(SETTINGS, webhooks=replace(SETTINGS.webhooks, allow_private_targets=True)),
    )
    with pytest.raises(PermissionDenied):
        webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:1/hook", ["ingest.started"])


# --- webhook delivery ---


class _Receiver:
    """Stands in for the subscriber's HTTP server: records deliveries, answers with `status`."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content, headers):
        self.calls.append((url, content, headers))
        import httpx as _httpx

        return _httpx.Response(self.status, request=_httpx.Request("POST", url))


def _local_webhooks(monkeypatch, **overrides):
    monkeypatch.setattr(
        webhooks,
        "SETTINGS",
        replace(
            SETTINGS,
            webhooks=replace(SETTINGS.webhooks, allow_private_targets=True, **overrides),
        ),
    )


def test_a_delivery_is_signed_with_the_subscription_secret(tenants, monkeypatch):
    import anyio

    _local_webhooks(monkeypatch)
    created = webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:9999/hook", [])
    receiver = _Receiver()
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", receiver)

    anyio.run(
        webhooks.dispatch, tenants.a.tenant_id, webhooks.INGEST_COMPLETED, {"doc_id": "d1"}
    )

    url, content, headers = receiver.calls[0]
    assert url == "http://127.0.0.1:9999/hook"
    assert json.loads(content)["event"] == webhooks.INGEST_COMPLETED
    assert headers[webhooks.SIGNATURE_HEADER] == webhooks.sign(
        created["secret"], headers[webhooks.TIMESTAMP_HEADER], content
    )


def test_a_tenants_event_never_reaches_another_tenants_subscription(tenants, monkeypatch):
    import anyio

    _local_webhooks(monkeypatch)
    webhooks.register(tenants.b.tenant_id, "http://127.0.0.1:9998/hook", [])
    receiver = _Receiver()
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", receiver)

    anyio.run(webhooks.dispatch, tenants.a.tenant_id, webhooks.INGEST_COMPLETED, {})
    assert receiver.calls == []


def test_a_run_of_failures_disables_the_subscription(tenants, monkeypatch):
    import anyio

    _local_webhooks(monkeypatch, max_attempts=1, max_failures=2, backoff_s=0)
    webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:9999/hook", [])
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _Receiver(status=500))

    anyio.run(webhooks.dispatch, tenants.a.tenant_id, webhooks.INGEST_COMPLETED, {})
    assert webhooks.listing(tenants.a.tenant_id)[0]["active"] is True

    anyio.run(webhooks.dispatch, tenants.a.tenant_id, webhooks.INGEST_COMPLETED, {})
    hook = webhooks.listing(tenants.a.tenant_id)[0]
    assert hook["active"] is False
    assert hook["last_error"] == "HTTP 500"


def test_a_success_clears_the_failure_run(tenants, monkeypatch):
    import anyio

    _local_webhooks(monkeypatch, max_attempts=1, max_failures=5, backoff_s=0)
    webhooks.register(tenants.a.tenant_id, "http://127.0.0.1:9999/hook", [])
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _Receiver(status=500))
    anyio.run(webhooks.dispatch, tenants.a.tenant_id, webhooks.INGEST_COMPLETED, {})
    assert webhooks.listing(tenants.a.tenant_id)[0]["consecutive_failures"] == 1

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _Receiver(status=200))
    anyio.run(webhooks.dispatch, tenants.a.tenant_id, webhooks.INGEST_COMPLETED, {})
    assert webhooks.listing(tenants.a.tenant_id)[0]["consecutive_failures"] == 0
