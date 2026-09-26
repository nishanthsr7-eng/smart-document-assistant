import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, router
from src.core.config import SETTINGS

PDF_BYTES = b"%PDF-1.4\n%mock pdf content\n"


@pytest.fixture
def client():
    return TestClient(router.app)


class _FakeVectorStore:
    """Only the query path reaches the store from the router; ingest owns its own writes."""

    def query(self, vector, k, doc_ids):
        return []


class _FakeEmbedder:
    def encode(self, texts):
        return [[0.0] for _ in texts]


@pytest.fixture
def fake_infra(monkeypatch):
    store = _FakeVectorStore()
    monkeypatch.setattr(deps, "vector_store", lambda: store)
    monkeypatch.setattr(deps, "embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(deps, "figure_captioner", lambda: None)
    return SimpleNamespace(store=store)


# --- POST /ingest ---


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, fn, doc_id, filename, job_id, _job_id, _queue_name):
        if any(job[3] == _job_id for job in self.enqueued):
            return None
        self.enqueued.append((fn, doc_id, filename, _job_id))
        return SimpleNamespace(job_id=_job_id)


@pytest.fixture
def fake_queue(monkeypatch):
    queue = _FakeQueue()
    state: dict[str, dict] = {}

    async def pool():
        return queue

    claims: dict[str, str] = {}

    def claim(doc_id: str, job_id: str):
        holder = claims.get(doc_id)
        if holder is None:
            claims[doc_id] = job_id
        return holder

    monkeypatch.setattr(router.jobs, "pool", pool)
    monkeypatch.setattr(router.jobs, "claim", claim)
    monkeypatch.setattr(router.jobs, "get", lambda job_id: state.get(job_id))
    monkeypatch.setattr(
        router.jobs,
        "record_queued",
        lambda job_id, doc_id, filename: state.update(
            {
                job_id: {
                    "job_id": job_id,
                    "doc_id": doc_id,
                    "filename": filename,
                    "status": "queued",
                    "stage": "Queued",
                }
            }
        ),
    )
    monkeypatch.setattr(router.objects, "put_raw", lambda doc_id, filename, data: "key")
    return SimpleNamespace(queue=queue, state=state)


def test_ingest_queues_a_job_instead_of_parsing_inline(client, fake_queue):
    resp = client.post("/ingest", files={"file": ("policy.pdf", PDF_BYTES, "application/pdf")})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["report"] is None
    assert body["job_id"] == f"ingest:{hashlib.sha256(PDF_BYTES).hexdigest()}"
    assert len(fake_queue.queue.enqueued) == 1
    assert fake_queue.queue.enqueued[0][0] == "ingest_job"


def test_ingest_of_in_flight_bytes_returns_the_same_job(client, fake_queue):
    first = client.post("/ingest", files={"file": ("policy.pdf", PDF_BYTES, "application/pdf")})
    second = client.post("/ingest", files={"file": ("policy.pdf", PDF_BYTES, "application/pdf")})

    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(fake_queue.queue.enqueued) == 1


def test_job_status_reports_the_finished_report(client, fake_queue):
    fake_queue.state["ingest:abc"] = {
        "job_id": "ingest:abc",
        "doc_id": "abc",
        "filename": "policy.pdf",
        "status": "done",
        "stage": "Indexed",
        "report": {
            "doc_id": "abc",
            "filename": "policy.pdf",
            "pages": 3,
            "num_parents": 2,
            "num_children": 4,
            "outcome": "indexed",
        },
    }

    resp = client.get("/jobs/ingest:abc")

    assert resp.status_code == 200
    assert resp.json()["report"]["num_children"] == 4


def test_job_status_unknown_job_is_404(client, fake_queue):
    assert client.get("/jobs/ingest:missing").status_code == 404


def test_ingest_rejects_oversize_file(client):
    huge = b"%PDF-1.4\n" + b"0" * (SETTINGS.ingestion.max_upload_mb * 1024 * 1024 + 1)
    resp = client.post("/ingest", files={"file": ("big.pdf", huge, "application/pdf")})
    assert resp.status_code == 400
    assert "MB upload limit" in resp.json()["detail"]


def test_ingest_rejects_wrong_extension(client):
    resp = client.post("/ingest", files={"file": ("notes.docx", b"hello", "application/octet-stream")})
    assert resp.status_code == 400
    assert "docx" in resp.json()["detail"]


def test_ingest_rejects_empty_file(client):
    resp = client.post("/ingest", files={"file": ("empty.pdf", b"", "application/pdf")})
    assert resp.status_code == 400
    assert "No text" in resp.json()["detail"]


# --- POST /query ---


def test_query_rejects_empty_question(client):
    resp = client.post("/query", json={"question": "   ", "doc_ids": []})
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"].lower()


def test_query_rejects_overlength_question(client):
    question = "a" * (SETTINGS.max_question_chars + 1)
    resp = client.post("/query", json={"question": question, "doc_ids": []})
    assert resp.status_code == 422


def test_query_rejects_unknown_mode(client):
    resp = client.post(
        "/query", json={"question": "What is the leave policy?", "doc_ids": [], "mode": "bogus"}
    )
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_query_rejects_special_character_heavy_question(client):
    resp = client.post("/query", json={"question": "$$$$$$@@@@@@!!!!!!####", "doc_ids": []})
    assert resp.status_code == 422
    assert "special characters" in resp.json()["detail"]


# --- GET /documents, DELETE /documents/{id} ---


def test_documents_list_and_delete_round_trip(client, fake_infra, monkeypatch):
    docs = [{"doc_id": "d1", "filename": "policy.pdf", "pages": 2, "num_children": 3}]
    monkeypatch.setattr(router, "list_indexed", lambda: docs)
    monkeypatch.setattr(router, "delete", lambda doc_id: None)

    list_resp = client.get("/documents")
    assert list_resp.status_code == 200
    assert list_resp.json() == docs

    delete_resp = client.delete("/documents/d1")
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {"status": "deleted", "doc_id": "d1"}
