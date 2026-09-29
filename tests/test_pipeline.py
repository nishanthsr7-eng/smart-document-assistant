import concurrent.futures

import pytest
from sqlalchemy import func, select

from src.auth.principal import Principal
from src.core.cache import ANSWER_CACHE
from src.core.config import SETTINGS
from src.ingestion import pipeline
from src.storage import objects
from src.storage.db import session
from src.storage.models import Chunk, Document, Tenant

DOC = b"1 Policy\n\nEmployees accrue leave each pay period. Sick leave is separate.\n\n- rule one\n- rule two"

pytestmark = pytest.mark.usefixtures("clean_state")

OWNER = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    tenant_id="00000000-0000-0000-0000-0000000000aa",
    email="owner@acme.test",
    role="admin",
)

DIM = SETTINGS.storage.embedding_dim


class _FakeEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[(len(t) % 7) / 7.0] * DIM for t in texts]


def _ingest_live(filename: str, data: bytes) -> pipeline.IngestReport:
    return pipeline.ingest(filename, data, _FakeEmbedder(), OWNER)


@pytest.fixture(autouse=True)
def owner_tenant(clean_state):
    """documents.tenant_id is a foreign key, so the owner's tenant must exist first."""
    with session() as sess:
        sess.add(Tenant(tenant_id=OWNER.tenant_id, name="pipeline-tests"))


def test_first_ingest_indexes(clean_state):
    report = _ingest_live("policy.txt", DOC)
    assert report.outcome == "indexed"
    assert report.num_children >= 1
    assert objects.get_parents(report.doc_id, SETTINGS.ingest_version)
    with session() as sess:
        assert sess.scalar(select(func.count()).select_from(Chunk)) == report.num_children


def test_reingest_same_bytes_is_duplicate(clean_state):
    first = _ingest_live("policy.txt", DOC)
    second = pipeline.ingest("policy.txt", DOC, _FakeEmbedder(), OWNER)
    assert second.outcome == "duplicate"
    assert second.doc_id == first.doc_id
    assert second.num_children == first.num_children


def test_same_filename_new_content_replaces(clean_state):
    first = _ingest_live("policy.txt", DOC)
    report = _ingest_live("policy.txt", DOC + b"\n\nExtra clause added here.")
    assert report.outcome == "replaced"
    assert report.replaced_doc_id == first.doc_id
    with session() as sess:
        # The replaced document is kept as history for the retention window, pointing at what
        # replaced it, but its index is gone: one filename, one answerable document.
        replaced = sess.get(Document, first.doc_id)
        assert replaced is not None
        assert replaced.superseded_by == report.doc_id
        assert sess.get(Document, report.doc_id).version == replaced.version + 1
        assert sess.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.doc_id == first.doc_id)
        ) == 0
    assert [e["doc_id"] for e in pipeline.list_indexed(OWNER.tenant_id)] == [report.doc_id]


def test_ingest_stores_embeddings_and_lists_the_document(clean_state):
    report = _ingest_live("policy.txt", DOC)
    assert [e["doc_id"] for e in pipeline.list_indexed(OWNER.tenant_id)] == [report.doc_id]
    with session() as sess:
        missing = sess.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.embedding.is_(None))
        )
    assert missing == 0


def test_list_indexed_reflects_documents(clean_state):
    report = _ingest_live("policy.txt", DOC)
    entries = {e["doc_id"]: e for e in pipeline.list_indexed(OWNER.tenant_id)}
    assert entries[report.doc_id]["filename"] == "policy.txt"
    assert entries[report.doc_id]["num_children"] == report.num_children


def test_delete_removes_rows_and_blobs(clean_state):
    report = _ingest_live("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)
    assert pipeline.list_indexed(OWNER.tenant_id) == []
    with session() as sess:
        assert sess.scalar(select(func.count()).select_from(Chunk)) == 0


def test_roundtrip_parents_and_children(clean_state):
    report = _ingest_live("policy.txt", DOC)
    parents = pipeline.load_parents(report.doc_id)
    children = pipeline.load_children(report.doc_id, OWNER.tenant_id)
    assert len(parents) == report.num_parents
    assert len(children) == report.num_children
    assert all(isinstance(c.section_path, tuple) for c in children)
    assert all(isinstance(c.char_span_in_parent, tuple) for c in children)


def test_ingest_invalidates_the_shared_answer_cache(clean_state):
    ANSWER_CACHE.set("k", {"answer": "stale"})
    _ingest_live("policy.txt", DOC)
    assert ANSWER_CACHE.get("k") is None


def test_concurrent_uploads_of_one_filename_keep_a_single_document(clean_state):
    """The old manifest was a read-modify-write with no lock; this raced and lost entries."""
    bodies = [DOC + f"\n\nVariant {i}.".encode() for i in range(4)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda b: _ingest_live("policy.txt", b), bodies))

    with session() as sess:
        live = list(sess.scalars(select(Document).where(Document.superseded_by.is_(None))))
        assert len(live) == 1
        assert live[0].doc_id in {r.doc_id for r in reports}
        # Only the surviving document has chunks: superseding drops the old index even though
        # it keeps the old row.
        orphans = sess.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.doc_id != live[0].doc_id)
        )
        assert orphans == 0


# --- ingest state machine ---


def test_ingest_ends_live_after_passing_through_every_state(clean_state, monkeypatch):
    seen: list[str] = []
    real = pipeline._set_state

    def record(doc_id, state, sess=None):
        seen.append(state)
        real(doc_id, state, sess)

    monkeypatch.setattr(pipeline, "_set_state", record)
    report = _ingest_live("policy.txt", DOC)

    assert seen == [pipeline.INDEXED, pipeline.LIVE]
    with session() as sess:
        assert sess.get(Document, report.doc_id).state == pipeline.LIVE


def test_a_crash_before_live_leaves_the_replaced_document_queryable(clean_state, monkeypatch):
    """The regression: the old order purged the replaced document first, so a crash mid-ingest
    left the tenant with neither the old document nor the new one."""
    first = _ingest_live("policy.txt", DOC)
    real = pipeline._set_state

    def fail_on_live(doc_id, state, sess=None):
        if state == pipeline.LIVE:
            raise RuntimeError("worker died between indexed and live")
        real(doc_id, state, sess)

    monkeypatch.setattr(pipeline, "_set_state", fail_on_live)
    with pytest.raises(RuntimeError):
        _ingest_live("policy.txt", DOC + b"\n\nExtra clause added here.")

    assert [e["doc_id"] for e in pipeline.list_indexed(OWNER.tenant_id)] == [first.doc_id]
    assert pipeline.load_children(first.doc_id, OWNER.tenant_id)


def test_chunks_and_the_indexed_state_commit_together(clean_state, monkeypatch):
    """Both halves of one transaction: a document may never claim an index that is not there."""
    real = pipeline._set_state

    def fail_on_indexed(doc_id, state, sess=None):
        if state == pipeline.INDEXED:
            raise RuntimeError("crash inside the index transaction")
        real(doc_id, state, sess)

    monkeypatch.setattr(pipeline, "_set_state", fail_on_indexed)
    with pytest.raises(RuntimeError):
        _ingest_live("policy.txt", DOC)

    with session() as sess:
        assert sess.scalar(select(func.count()).select_from(Chunk)) == 0
        assert sess.scalar(select(Document.state)) == pipeline.PARSED


def test_a_half_finished_ingest_is_never_listed(clean_state, monkeypatch):
    monkeypatch.setattr(
        pipeline, "chunk_document", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("parse died"))
    )
    with pytest.raises(RuntimeError):
        _ingest_live("policy.txt", DOC)

    assert pipeline.list_indexed(OWNER.tenant_id) == []
    with session() as sess:
        # The row is the trace of where it stopped, and discard_failed is what sweeps it.
        assert sess.scalar(select(Document.state)) == pipeline.PENDING
    pipeline.discard_failed(pipeline.doc_id_for(OWNER.tenant_id, DOC))
    with session() as sess:
        assert sess.scalar(select(func.count()).select_from(Document)) == 0
