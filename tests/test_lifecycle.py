"""Item 22: what happens to a document after it is indexed.

The three properties worth pinning here are the ones whose absence is silent. A delete that
cannot be undone looks identical to one that can until someone needs to undo it. A retention
window that never fires looks identical to one that does until the disk fills. And a chunker
change that empties the corpus looks, from `/documents`, exactly like a corpus that is empty.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from src.auth.principal import Principal
from src.core.config import SETTINGS, replace
from src.core.errors import DocumentNotFound, PermissionDenied
from src.ingestion import pipeline, reindex
from src.storage import alias
from src.storage.db import session
from src.storage.models import Chunk, Document, IndexAlias, Tenant

pytestmark = pytest.mark.usefixtures("clean_state")

DOC = b"1 Policy\n\nEmployees accrue leave each pay period. Sick leave is separate."
OTHER = b"1 Handbook\n\nExpenses are reimbursed within thirty days of the claim."

OWNER = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    tenant_id="00000000-0000-0000-0000-0000000000aa",
    email="owner@acme.test",
    role="admin",
)
COLLEAGUE = Principal(**{**OWNER.__dict__, "user_id": "00000000-0000-0000-0000-000000000002", "role": "editor"})

DIM = SETTINGS.storage.embedding_dim


class _FakeEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[(len(t) % 7) / 7.0] * DIM for t in texts]


@pytest.fixture(autouse=True)
def owner_tenant(clean_state):
    with session() as sess:
        sess.add(Tenant(tenant_id=OWNER.tenant_id, name="lifecycle-tests"))
    _reset_alias()
    yield
    _reset_alias()


def _reset_alias() -> None:
    """The alias outlives `clean_state`, which only clears tenants and documents."""
    with session() as sess:
        sess.execute(update(IndexAlias).values(
            ingest_version=SETTINGS.ingest_version, previous_version=None
        ))
    alias._invalidate()


def _ingest(filename: str, data: bytes) -> pipeline.IngestReport:
    return pipeline.ingest(filename, data, _FakeEmbedder(), OWNER)


def _chunk_count(doc_id: str) -> int:
    with session() as sess:
        return sess.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)
        )


def _listed() -> list[str]:
    return [d["doc_id"] for d in pipeline.list_indexed(OWNER.tenant_id)]


# --- soft delete ---


def test_delete_stops_answers_immediately_but_keeps_the_document(clean_state):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)

    # Unanswerable now: the index is what makes a document reachable, and it is gone.
    assert _chunk_count(report.doc_id) == 0
    assert _listed() == []
    assert pipeline.scope_doc_ids([report.doc_id], OWNER.tenant_id) == []
    # Recoverable until the window closes: the row and the stored bytes are still there.
    with session() as sess:
        row = sess.get(Document, report.doc_id)
    assert row is not None and row.deleted_at is not None


def test_deleting_twice_is_a_404(clean_state):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)
    with pytest.raises(DocumentNotFound):
        pipeline.delete(report.doc_id, OWNER)


def test_only_the_owner_or_an_admin_may_delete(clean_state):
    report = _ingest("policy.txt", DOC)
    with pytest.raises(PermissionDenied):
        pipeline.delete(report.doc_id, COLLEAGUE)


def test_restore_undoes_a_delete_inside_the_window(clean_state):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)

    restored = pipeline.restore(report.doc_id, OWNER)

    assert restored["filename"] == "policy.txt"
    with session() as sess:
        assert sess.get(Document, report.doc_id).deleted_at is None
    # Not yet listed: the bytes came back, the index did not, which is why restore returns a
    # job rather than a document.
    assert _listed() == []


def test_restoring_something_that_was_not_deleted_is_a_404(clean_state):
    report = _ingest("policy.txt", DOC)
    with pytest.raises(DocumentNotFound):
        pipeline.restore(report.doc_id, OWNER)


def test_re_uploading_a_deleted_document_brings_it_back(clean_state):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)

    again = _ingest("policy.txt", DOC)

    # The id is the content hash, so this is the same row undeleted, not a second document.
    assert again.doc_id == report.doc_id
    assert _listed() == [report.doc_id]


# --- versions ---


def test_replacing_a_file_keeps_the_version_it_replaced(clean_state):
    _ingest("policy.txt", DOC)
    second = _ingest("policy.txt", DOC + b"\n\nAmended.")

    history = pipeline.versions(second.doc_id, OWNER.tenant_id)

    assert [h["version"] for h in history] == [2, 1]
    assert history[0]["current"] is True
    assert history[1]["superseded_by"] == second.doc_id
    assert history[1]["current"] is False


def test_only_the_current_version_is_answerable(clean_state):
    first = _ingest("policy.txt", DOC)
    second = _ingest("policy.txt", DOC + b"\n\nAmended.")

    assert _listed() == [second.doc_id]
    assert _chunk_count(first.doc_id) == 0


def test_a_third_upload_supersedes_the_second_not_the_first(clean_state):
    _ingest("policy.txt", DOC)
    second = _ingest("policy.txt", DOC + b"\n\nAmended.")
    third = _ingest("policy.txt", DOC + b"\n\nAmended twice.")

    history = {h["doc_id"]: h for h in pipeline.versions(third.doc_id, OWNER.tenant_id)}

    assert history[second.doc_id]["superseded_by"] == third.doc_id
    assert history[third.doc_id]["version"] == 3


def test_versions_of_another_tenants_document_is_a_404(tenants):
    with pytest.raises(DocumentNotFound):
        pipeline.versions("whatever", tenants.b.tenant_id)


# --- retention ---


def test_the_sweep_purges_a_delete_past_its_window(clean_state):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)
    _age(report.doc_id, deleted_days=SETTINGS.retention.soft_delete_days + 1)

    result = pipeline.sweep_expired()

    assert result["documents_purged"] == 1
    with session() as sess:
        assert sess.get(Document, report.doc_id) is None


def test_the_sweep_leaves_a_delete_inside_its_window(clean_state):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)
    _age(report.doc_id, deleted_days=1)

    assert pipeline.sweep_expired()["documents_purged"] == 0
    with session() as sess:
        assert sess.get(Document, report.doc_id) is not None


def test_the_sweep_purges_a_superseded_version_past_its_window(clean_state):
    first = _ingest("policy.txt", DOC)
    second = _ingest("policy.txt", DOC + b"\n\nAmended.")
    _age(first.doc_id, created_days=SETTINGS.retention.superseded_days + 1)

    pipeline.sweep_expired()

    with session() as sess:
        assert sess.get(Document, first.doc_id) is None
        assert sess.get(Document, second.doc_id) is not None


def test_the_sweep_is_idempotent(clean_state):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)
    _age(report.doc_id, deleted_days=SETTINGS.retention.soft_delete_days + 1)

    assert pipeline.sweep_expired()["documents_purged"] == 1
    assert pipeline.sweep_expired()["documents_purged"] == 0


def test_a_zero_window_keeps_things_forever(clean_state, monkeypatch):
    report = _ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id, OWNER)
    _age(report.doc_id, deleted_days=9999)
    monkeypatch.setattr(
        pipeline,
        "SETTINGS",
        replace(SETTINGS, retention=replace(SETTINGS.retention, soft_delete_days=0)),
    )

    assert pipeline.sweep_expired()["documents_purged"] == 0


def _age(doc_id: str, deleted_days: int = 0, created_days: int = 0) -> None:
    now = datetime.now(timezone.utc)
    values = {}
    if deleted_days:
        values["deleted_at"] = now - timedelta(days=deleted_days)
    if created_days:
        values["created_at"] = now - timedelta(days=created_days)
    with session() as sess:
        sess.execute(update(Document).where(Document.doc_id == doc_id).values(**values))


# --- the index alias ---


def test_reads_follow_the_alias_not_the_running_configuration(clean_state):
    """The defect this exists to stop: change the chunker and the corpus silently empties."""
    report = _ingest("policy.txt", DOC)
    assert _listed() == [report.doc_id]

    # A configuration change: the running process would now build a different index.
    with session() as sess:
        sess.execute(
            update(Document).where(Document.doc_id == report.doc_id).values(ingest_version="old")
        )
        sess.execute(update(Chunk).where(Chunk.doc_id == report.doc_id).values(ingest_version="old"))
        sess.execute(update(IndexAlias).values(ingest_version="old"))
    alias._invalidate()

    # Still readable, because the alias still names the build that is actually indexed.
    assert _listed() == [report.doc_id]
    assert alias.state()["reindex_needed"] is True


def test_a_stale_document_is_on_the_reindex_worklist(clean_state):
    report = _ingest("policy.txt", DOC)
    assert pipeline.stale_documents() == []

    _pretend_indexed_at("old", report.doc_id)

    # The worklist is "not built at the target version", so a rebuild interrupted half way
    # resumes rather than starting over.
    worklist = pipeline.stale_documents()
    assert [entry["doc_id"] for entry in worklist] == [report.doc_id]
    assert worklist[0]["from_version"] == "old"


def test_a_rebuild_writes_beside_the_live_index_and_only_then_cuts_over(clean_state):
    report = _ingest("policy.txt", DOC)
    _pretend_indexed_at("old", report.doc_id)

    summary = reindex.run(_FakeEmbedder())

    assert summary["promoted"] is True
    assert summary["done"] == 1
    assert alias.state()["active_version"] == SETTINGS.ingest_version
    # Both builds' chunks are present: the old ones are the rollback until they are swept.
    with session() as sess:
        builds = set(sess.scalars(select(Chunk.ingest_version).distinct()))
    assert builds == {"old", SETTINGS.ingest_version}
    assert _listed() == [report.doc_id]


def test_a_rebuild_that_could_not_finish_does_not_cut_over(clean_state, monkeypatch):
    """A partial cutover would hide every document the rebuild could not produce, which is the
    silent disappearance the alias exists to prevent."""
    report = _ingest("policy.txt", DOC)
    _pretend_indexed_at("old", report.doc_id)

    def explode(*args, **kwargs):
        raise RuntimeError("parser died")

    monkeypatch.setattr(reindex, "reindex_document", explode)
    summary = reindex.run(_FakeEmbedder())

    assert summary["promoted"] is False
    assert summary["failed"] == 1
    assert alias.state()["active_version"] == "old"
    # And the document is still being served by the build that works.
    assert _listed() == [report.doc_id]


def test_rollback_points_back_at_the_previous_build(clean_state):
    report = _ingest("policy.txt", DOC)
    _pretend_indexed_at("old", report.doc_id)
    reindex.run(_FakeEmbedder())

    alias.rollback()

    assert alias.state()["active_version"] == "old"
    assert _listed() == [report.doc_id]


def test_rollback_with_nowhere_to_go_is_refused(clean_state):
    with pytest.raises(LookupError):
        alias.rollback()


def test_the_sweep_keeps_the_old_build_until_the_rollback_window_passes(clean_state):
    report = _ingest("policy.txt", DOC)
    _pretend_indexed_at("old", report.doc_id)
    reindex.run(_FakeEmbedder())

    assert pipeline.sweep_expired()["chunks_purged"] == 0

    # The clock runs from the cutover, not from when the documents were uploaded.
    with session() as sess:
        sess.execute(
            update(IndexAlias).values(
                updated_at=datetime.now(timezone.utc)
                - timedelta(days=SETTINGS.retention.old_index_days + 1)
            )
        )

    assert pipeline.sweep_expired()["chunks_purged"] > 0
    with session() as sess:
        assert set(sess.scalars(select(Chunk.ingest_version).distinct())) == {
            SETTINGS.ingest_version
        }


def _pretend_indexed_at(version: str, doc_id: str) -> None:
    """Move a document and the alias to an older build, as a configuration change would.

    Chunk ids carry their build, so they are rewritten too -- otherwise the rebuild would
    collide with these rows instead of landing beside them, and the test would be measuring
    something that cannot happen in a real reindex.
    """
    current = f"@{SETTINGS.ingest_version}:"
    with session() as sess:
        sess.execute(update(Document).where(Document.doc_id == doc_id).values(ingest_version=version))
        sess.execute(
            update(Chunk)
            .where(Chunk.doc_id == doc_id)
            .values(
                ingest_version=version,
                chunk_id=func.replace(Chunk.chunk_id, current, f"@{version}:"),
                parent_id=func.replace(Chunk.parent_id, current, f"@{version}:"),
            )
        )
        sess.execute(update(IndexAlias).values(ingest_version=version, previous_version=None))
    alias._invalidate()


# --- backup and restore ---


def test_object_deletes_are_batched_under_the_api_limit(monkeypatch):
    """S3's DeleteObjects rejects a request with more than 1000 keys outright. A bucket small
    enough for a unit test never reaches that, which is why the first restore drill against a
    real one failed here."""
    from src.storage import objects

    calls = []

    class _Client:
        def delete_objects(self, Bucket, Delete):
            calls.append(len(Delete["Objects"]))

    monkeypatch.setattr(objects, "_client", lambda: _Client())
    objects.delete_keys([f"k{i}" for i in range(2500)])

    assert calls == [1000, 1000, 500]


def test_the_sweep_collects_a_row_left_by_a_killed_ingest(clean_state):
    """`discard_failed` runs in the failure path, so a job that raises cleans up after itself.
    A SIGKILLed one does not, and left a row short of `live` that nothing collected."""
    report = _ingest("policy.txt", DOC)
    with session() as sess:
        sess.execute(
            update(Document)
            .where(Document.doc_id == report.doc_id)
            .values(
                state="pending",
                created_at=datetime.now(timezone.utc)
                - timedelta(seconds=SETTINGS.jobs.job_timeout_s * 3),
            )
        )

    assert pipeline.sweep_expired()["abandoned_purged"] == 1
    with session() as sess:
        assert sess.get(Document, report.doc_id) is None


def test_the_sweep_leaves_an_ingest_that_is_still_running(clean_state):
    report = _ingest("policy.txt", DOC)
    with session() as sess:
        sess.execute(
            update(Document).where(Document.doc_id == report.doc_id).values(state="parsed")
        )

    assert pipeline.sweep_expired()["abandoned_purged"] == 0
