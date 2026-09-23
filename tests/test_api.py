from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, router
from src.core.config import SETTINGS
from src.ingestion.pipeline import IngestReport

PDF_BYTES = b"%PDF-1.4\n%mock pdf content\n"


@pytest.fixture
def client():
    return TestClient(router.app)


class _FakeVectorStore:
    def __init__(self):
        self.added = []
        self.deleted = []

    def has_doc(self, doc_id):
        return False

    def add(self, children, embeddings):
        self.added.append((children, embeddings))

    def delete_doc(self, doc_id):
        self.deleted.append(doc_id)


class _FakeKeywordIndex:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_doc(self, children):
        self.added.append(children)

    def remove_doc(self, doc_id):
        self.removed.append(doc_id)


class _FakeEmbedder:
    def encode(self, texts):
        return [[0.0] for _ in texts]


@pytest.fixture
def fake_infra(monkeypatch):
    store = _FakeVectorStore()
    index = _FakeKeywordIndex()
    monkeypatch.setattr(deps, "vector_store", lambda: store)
    monkeypatch.setattr(deps, "keyword_index", lambda: index)
    monkeypatch.setattr(deps, "embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(deps, "figure_captioner", lambda: None)
    return SimpleNamespace(store=store, index=index)


# --- POST /ingest ---


def test_ingest_happy_path(client, fake_infra, monkeypatch):
    report = IngestReport(
        doc_id="abc123",
        filename="policy.pdf",
        pages=3,
        elements_by_kind={"paragraph": 5},
        num_parents=2,
        num_children=4,
        outcome="indexed",
    )
    monkeypatch.setattr(router, "ingest", lambda filename, data, captioner=None: report)
    monkeypatch.setattr(router, "load_children", lambda doc_id: [])

    resp = client.post("/ingest", files={"file": ("policy.pdf", PDF_BYTES, "application/pdf")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == "abc123"
    assert body["outcome"] == "indexed"
    assert body["num_children"] == 4
    assert fake_infra.store.added
    assert fake_infra.index.added


def test_ingest_purges_superseded_doc(client, fake_infra, monkeypatch):
    report = IngestReport(
        doc_id="new1",
        filename="policy.pdf",
        pages=1,
        elements_by_kind={},
        num_parents=1,
        num_children=1,
        outcome="replaced",
        replaced_doc_id="old1",
    )
    monkeypatch.setattr(router, "ingest", lambda filename, data, captioner=None: report)
    monkeypatch.setattr(router, "load_children", lambda doc_id: [])

    resp = client.post("/ingest", files={"file": ("policy.pdf", PDF_BYTES, "application/pdf")})

    assert resp.status_code == 200
    assert fake_infra.store.deleted == ["old1"]
    assert fake_infra.index.removed == ["old1"]


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
    assert fake_infra.store.deleted == ["d1"]
    assert fake_infra.index.removed == ["d1"]
