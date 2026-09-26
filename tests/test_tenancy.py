"""Cross-tenant isolation. Every assertion here is "tenant A cannot reach tenant B's data"."""

import pytest
from sqlalchemy import func, select

from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.core.errors import DocumentNotFound, PermissionDenied
from src.generation import answerer
from src.ingestion import pipeline
from src.retrieval.keyword_index import KeywordIndex
from src.retrieval.vector_store import VectorStore
from src.storage.db import session
from src.storage.models import Chunk, Document

DIM = SETTINGS.storage.embedding_dim

SECRET_A = (
    b"1 Compensation\n\n"
    b"The Acme quarterly bonus pool is 4200000 dollars, allocated by the compensation committee.\n"
)
SECRET_B = (
    b"1 Compensation\n\n"
    b"The Globex quarterly bonus pool is 9900000 dollars, allocated by the compensation committee.\n"
)

pytestmark = pytest.mark.usefixtures("clean_state")


class _FakeEmbedder:
    """Deterministic and identical for both tenants: nothing here depends on vector distance."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * DIM for _ in texts]

    def encode_query(self, texts: list[str]) -> list[list[float]]:
        return self.encode(texts)


def _ingest(owner: Principal, filename: str, data: bytes) -> pipeline.IngestReport:
    return pipeline.ingest(filename, data, _FakeEmbedder(), owner)


# --- identity of a document ---


def test_same_bytes_in_two_tenants_are_two_documents(tenants):
    a = _ingest(tenants.a, "policy.txt", SECRET_A)
    b = _ingest(tenants.b, "policy.txt", SECRET_A)

    assert a.doc_id != b.doc_id
    assert a.outcome == "indexed" and b.outcome == "indexed"
    with session() as sess:
        assert sess.scalar(select(func.count()).select_from(Document)) == 2


def test_replace_by_filename_never_crosses_a_tenant(tenants):
    """The pre-tenancy bug: an upload replaced any document that shared its filename."""
    a = _ingest(tenants.a, "policy.txt", SECRET_A)
    b = _ingest(tenants.b, "policy.txt", SECRET_B)

    assert b.outcome == "indexed"
    assert b.replaced_doc_id is None
    assert [d["doc_id"] for d in pipeline.list_indexed(tenants.a.tenant_id)] == [a.doc_id]
    assert [d["doc_id"] for d in pipeline.list_indexed(tenants.b.tenant_id)] == [b.doc_id]


# --- listing and deletion ---


def test_list_indexed_shows_only_the_callers_tenant(tenants):
    _ingest(tenants.a, "a.txt", SECRET_A)
    _ingest(tenants.b, "b.txt", SECRET_B)

    assert [d["filename"] for d in pipeline.list_indexed(tenants.a.tenant_id)] == ["a.txt"]
    assert [d["filename"] for d in pipeline.list_indexed(tenants.b.tenant_id)] == ["b.txt"]


def test_delete_of_another_tenants_document_is_not_found(tenants):
    a = _ingest(tenants.a, "a.txt", SECRET_A)

    with pytest.raises(DocumentNotFound):
        pipeline.delete(a.doc_id, tenants.b)

    assert [d["doc_id"] for d in pipeline.list_indexed(tenants.a.tenant_id)] == [a.doc_id]


def test_viewer_cannot_delete_another_users_document_in_the_same_tenant(tenants):
    from src.auth import service

    a = _ingest(tenants.a, "a.txt", SECRET_A)
    colleague = service.create_user(tenants.a, "editor@acme.test", "acme-password-2", "editor")

    with pytest.raises(PermissionDenied):
        pipeline.delete(a.doc_id, colleague)

    # The tenant's admin can, because an admin owns its tenant's corpus.
    pipeline.delete(a.doc_id, tenants.a)
    assert pipeline.list_indexed(tenants.a.tenant_id) == []


def test_scope_doc_ids_drops_foreign_ids(tenants):
    a = _ingest(tenants.a, "a.txt", SECRET_A)
    b = _ingest(tenants.b, "b.txt", SECRET_B)

    assert pipeline.scope_doc_ids([a.doc_id, b.doc_id], tenants.a.tenant_id) == [a.doc_id]
    assert pipeline.scope_doc_ids([a.doc_id], tenants.b.tenant_id) == []


# --- retrieval: the one test auditors ask for ---


def test_dense_retrieval_scoped_to_a_never_returns_a_chunk_of_b(tenants):
    """Both documents embed to the same vector, so only the tenant predicate separates them."""
    a = _ingest(tenants.a, "a.txt", SECRET_A)
    b = _ingest(tenants.b, "b.txt", SECRET_B)
    store = VectorStore()
    vector = _FakeEmbedder().encode_query(["bonus pool"])[0]

    # Passing B's doc_id explicitly is the attack: the tenant predicate must still win.
    hits = store.query(vector, 30, [a.doc_id, b.doc_id], tenants.a.tenant_id)

    assert hits
    assert {hit.doc_id for hit in hits} == {a.doc_id}
    assert not any(b"9900000" in hit.text.encode() for hit in hits)


def test_lexical_retrieval_scoped_to_a_never_returns_a_chunk_of_b(tenants):
    a = _ingest(tenants.a, "a.txt", SECRET_A)
    b = _ingest(tenants.b, "b.txt", SECRET_B)
    index = KeywordIndex()

    hits = index.query("quarterly bonus pool", 30, [a.doc_id, b.doc_id], tenants.a.tenant_id)

    assert hits
    assert {hit.doc_id for hit in hits} == {a.doc_id}


def test_get_embeddings_is_tenant_scoped(tenants):
    _ingest(tenants.a, "a.txt", SECRET_A)
    b = _ingest(tenants.b, "b.txt", SECRET_B)
    with session() as sess:
        b_chunks = list(sess.scalars(select(Chunk.chunk_id).where(Chunk.doc_id == b.doc_id)))

    assert VectorStore().get_embeddings(b_chunks, tenants.a.tenant_id) == {}
    assert VectorStore().get_embeddings(b_chunks, tenants.b.tenant_id)


def test_chunks_carry_the_owning_tenant(tenants):
    a = _ingest(tenants.a, "a.txt", SECRET_A)
    with session() as sess:
        tenant_ids = set(sess.scalars(select(Chunk.tenant_id).where(Chunk.doc_id == a.doc_id)))
    assert tenant_ids == {tenants.a.tenant_id}


# --- answer cache ---


def test_answer_cache_key_is_tenant_scoped(tenants):
    """Two tenants asking the same question of same-named documents must not share an answer."""
    key_a = answerer._cache_key(tenants.a.tenant_id, "What is the bonus pool?", ["d"], "hybrid", True)
    key_b = answerer._cache_key(tenants.b.tenant_id, "What is the bonus pool?", ["d"], "hybrid", True)
    assert key_a != key_b


# --- job state ---


def test_job_state_of_another_tenant_reads_as_missing(tenants):
    from src.ingestion import jobs

    doc_id = pipeline.doc_id_for(tenants.a.tenant_id, SECRET_A)
    job_id = jobs.job_id_for(doc_id)
    jobs.record_queued(job_id, doc_id, "a.txt", tenants.a)

    assert jobs.get(job_id, tenants.a.tenant_id)["status"] == jobs.QUEUED
    assert jobs.get(job_id, tenants.b.tenant_id) is None


def test_dead_letters_are_filtered_by_tenant(tenants):
    from src.ingestion import jobs
    from src.storage.redis_client import client

    client().delete(SETTINGS.jobs.dlq_key)
    jobs.record_failed("job:a", "doc-a", "a.txt", "boom", tenants.a.tenant_id)
    jobs.record_failed("job:b", "doc-b", "b.txt", "boom", tenants.b.tenant_id)

    assert [e["job_id"] for e in jobs.dead_letters(tenants.a.tenant_id)] == ["job:a"]
    assert [e["job_id"] for e in jobs.dead_letters(tenants.b.tenant_id)] == ["job:b"]
