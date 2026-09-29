import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import IO, Literal, Optional, Protocol

from sqlalchemy import ColumnElement, and_, func, or_, select, tuple_, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.auth.principal import Principal
from src.core.cache import ANSWER_CACHE, DOC_CACHE
from src.core.config import SETTINGS
from src.core.errors import DocumentNotFound, PermissionDenied
from src.core.tracing import OnStage
from src.ingestion import upload
from src.ingestion.chunker import ChildChunk, ParentChunk, chunk_document
from src.ingestion.parsers import Element, FigureCaptioner, parse, validate_upload
from src.retrieval.vector_store import VectorStore
from src.storage import alias, objects
from src.storage.db import session
from src.storage.models import Chunk, Document
from src.storage.redis_client import lock

Outcome = Literal["indexed", "replaced", "duplicate"]

# Ingest is a state machine, and only the last state is visible to a reader. Where a crashed job
# stopped is readable from the row it left behind, and anything short of `live` is swept by
# `discard_failed`: pending (row and blobs staged) -> parsed (chunked, parents stored) ->
# indexed (chunks and embeddings committed) -> live (queryable, replacement retired).
State = Literal["pending", "parsed", "indexed", "live"]
PENDING: State = "pending"
PARSED: State = "parsed"
INDEXED: State = "indexed"
LIVE: State = "live"


def _doc_id_salt(tenant_id: str) -> bytes:
    return tenant_id.encode() + b"\x00"


def doc_id_for(tenant_id: str, data: bytes) -> str:
    """Tenant-salted content hash: identical bytes uploaded by two tenants are two
    documents, and no tenant can name another's document by hashing a file it already has.
    """
    return hashlib.sha256(_doc_id_salt(tenant_id) + data).hexdigest()


def doc_id_for_stream(tenant_id: str, body: IO[bytes]) -> str:
    """The same id for a body that is never fully resident: the API has a spooled file and the
    worker has bytes, and the two must agree on the id."""
    return upload.digest(_doc_id_salt(tenant_id), body)


class Embedding(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class IngestReport:
    doc_id: str
    filename: str
    pages: int
    elements_by_kind: dict[str, int]
    num_parents: int
    num_children: int
    outcome: Outcome
    replaced_doc_id: Optional[str] = None


def ingest(
    filename: str,
    data: bytes,
    embedder: Embedding,
    owner: Principal,
    captioner: Optional[FigureCaptioner] = None,
    on_stage: Optional[OnStage] = None,
) -> IngestReport:
    """Parse, chunk, embed and index one document as a single atomic unit.

    Embedding runs inside the lock: releasing it before the index write let a concurrent upload
    of the same filename purge the document row while this one was still embedding, and the
    chunk insert then failed on the foreign key.
    """
    extension = validate_upload(filename, data)
    doc_id = doc_id_for(owner.tenant_id, data)

    # Serialized across workers: the replace-by-filename path is a read-modify-write on the
    # document set, and two concurrent uploads of the same name would otherwise interleave.
    # Keyed per tenant so one tenant's uploads never block another's.
    with lock(f"ingest:{owner.tenant_id}:{filename}", SETTINGS.storage.ingest_lock_ttl_s):
        existing = _live_document(doc_id, owner.tenant_id)
        if existing is not None:
            return IngestReport(
                doc_id=doc_id,
                filename=existing.filename,
                pages=existing.pages,
                elements_by_kind=existing.elements_by_kind,
                num_parents=existing.num_parents,
                num_children=existing.num_children,
                outcome="duplicate",
            )

        replaced = _find_by_filename(filename, doc_id, owner.tenant_id)

        _begin_document(doc_id, filename, owner, version=_next_version(replaced))

        _emit(on_stage, "Parsing document")
        elements = parse(extension, data, captioner)

        figures = sum(1 for e in elements if e.kind == "figure")
        if figures and captioner is not None:
            _emit(on_stage, f"Captioning {figures} figure(s)")

        _emit(on_stage, "Chunking")
        build = SETTINGS.ingest_version
        parents, children = chunk_document(elements, doc_id, filename, build)

        # Blobs before rows, always: a blob with no row is dead weight that `discard_failed`
        # sweeps, while a row with no blob is a live document whose context cannot be assembled.
        objects.put_raw(doc_id, filename, data)
        objects.put_parents(
            doc_id, build, json.dumps({"parents": [asdict(p) for p in parents]}).encode()
        )
        DOC_CACHE.delete(_parent_cache_key(doc_id, build))

        report = IngestReport(
            doc_id=doc_id,
            filename=filename,
            pages=_page_count(elements),
            elements_by_kind=dict(Counter(e.kind for e in elements)),
            num_parents=len(parents),
            num_children=len(children),
            outcome="replaced" if replaced else "indexed",
            replaced_doc_id=replaced,
        )
        _upsert_document(report, owner, PARSED)

        _emit(on_stage, f"Embedding {len(children)} chunks")
        embeddings = embedder.encode([c.embed_text for c in children])

        _emit(on_stage, f"Indexing {len(children)} chunks")
        # One transaction: the chunks and the state that claims them. An outbox would be the
        # answer if the index were a separate system, but it is this same Postgres, so the
        # write that has to be atomic already can be.
        with session() as sess:
            VectorStore().add(children, embeddings, owner.tenant_id, sess=sess, ingest_version=build)
            _set_state(doc_id, INDEXED, sess)

        # Live last, and the replaced document is purged only after that. The old order deleted
        # it first, so a crash mid-ingest left the tenant with neither document; now the window
        # shows both, which is the failure worth having.
        _set_state(doc_id, LIVE)
        if replaced:
            # Superseded, not destroyed. The old row points at the new one and keeps its bytes
            # for the retention window, so "which version answered that" has an answer and a
            # bad replacement can be rolled back. Its chunks go now: one filename, one index.
            _supersede(replaced, doc_id)

    ANSWER_CACHE.clear()
    return report


def _set_state(doc_id: str, state: State, sess: Optional[Session] = None) -> None:
    stmt = update(Document).where(Document.doc_id == doc_id).values(state=state)
    if sess is not None:
        sess.execute(stmt)
        return
    with session() as own:
        own.execute(stmt)


def list_indexed(
    tenant_id: str,
    limit: Optional[int] = None,
    after: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """One page of the tenant's live documents, oldest first.

    `after` is a keyset, not an offset: `(created_at, doc_id)` is unique and monotonic, so a
    document ingested while a client is paging cannot make it skip a row.
    """
    with session() as sess:
        stmt = (
            select(Document)
            .where(_listable(tenant_id))
            .order_by(Document.created_at, Document.doc_id)
        )
        if after is not None:
            stmt = stmt.where(tuple_(Document.created_at, Document.doc_id) > after)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            {
                "doc_id": doc.doc_id,
                "filename": doc.filename,
                "pages": doc.pages,
                "num_children": doc.num_children,
                "owner_id": doc.owner_id,
                "created_at": doc.created_at,
            }
            for doc in sess.scalars(stmt)
        ]


def scope_doc_ids(doc_ids: list[str], tenant_id: str) -> list[str]:
    """Narrow caller-supplied doc_ids to the tenant's live documents.

    A foreign or unknown id is dropped rather than rejected: a 404 here would tell the
    caller whether that id exists in some other tenant.
    """
    if not doc_ids:
        return []
    with session() as sess:
        stmt = select(Document.doc_id).where(
            _listable(tenant_id), Document.doc_id.in_(doc_ids)
        )
        return list(sess.scalars(stmt))


def delete(doc_id: str, actor: Principal) -> None:
    """Soft-delete a document in the actor's own tenant. A foreign doc_id is a 404.

    Two different dates, and the gap between them is the point: the document stops being
    answerable now -- its chunks go with this call -- and the row and the stored bytes survive
    `retention.soft_delete_days`, so a delete made in error is recoverable until then. Erasure
    under a data-protection request is the other operation, in `storage/erasure.py`, and that
    one is immediate and verified.
    """
    document = _document(doc_id, actor.tenant_id)
    if document is None or document.deleted_at is not None:
        raise DocumentNotFound(doc_id)
    if not actor.can("admin") and document.owner_id != actor.user_id:
        raise PermissionDenied("Only the document's owner or an admin can delete it.")
    with session() as sess:
        sess.execute(
            update(Document)
            .where(Document.doc_id == doc_id)
            .values(deleted_at=datetime.now(timezone.utc))
        )
        sess.execute(sa_delete(Chunk).where(Chunk.doc_id == doc_id))
    ANSWER_CACHE.clear()


def restore(doc_id: str, actor: Principal) -> dict:
    """Undo a soft delete, inside the window. The bytes are still there; the index is not, so
    the caller gets a reindex job rather than a document that is instantly answerable again."""
    document = _document(doc_id, actor.tenant_id)
    if document is None or document.deleted_at is None:
        raise DocumentNotFound(doc_id)
    with session() as sess:
        sess.execute(
            update(Document)
            .where(Document.doc_id == doc_id)
            .values(deleted_at=None, state=PARSED)
        )
    return {"doc_id": doc_id, "filename": document.filename}


def versions(doc_id: str, tenant_id: str) -> list[dict]:
    """Every upload of this document's filename, newest first: what replaced what, and when."""
    document = _document(doc_id, tenant_id)
    if document is None:
        raise DocumentNotFound(doc_id)
    with session() as sess:
        stmt = (
            select(Document)
            .where(Document.tenant_id == tenant_id, Document.filename == document.filename)
            .order_by(Document.version.desc(), Document.created_at.desc())
        )
        return [
            {
                "doc_id": row.doc_id,
                "version": row.version,
                "pages": row.pages,
                "num_children": row.num_children,
                "created_at": row.created_at.isoformat(),
                "superseded_by": row.superseded_by,
                "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
                "current": row.superseded_by is None and row.deleted_at is None,
            }
            for row in sess.scalars(stmt)
        ]


def discard_failed(doc_id: str) -> None:
    """Drop the half-written rows and blobs of a failed ingest. Never touches a live document."""
    with session() as sess:
        sess.execute(sa_delete(Document).where(Document.doc_id == doc_id, Document.state != LIVE))
    objects.delete_doc(doc_id)


def load_parents(doc_id: str) -> list[ParentChunk]:
    """Only ever called with doc_ids that came back from tenant-filtered retrieval.

    Read at the alias, like the chunks that produced the hit: after a cutover the previous
    build's parents are still in the object store, and serving them next to the new build's
    spans would put the citation offsets on the wrong text.
    """
    build = alias.active_version()
    key = _parent_cache_key(doc_id, build)
    payload = DOC_CACHE.get(key)
    if payload is None:
        payload = json.loads(objects.get_parents(doc_id, build))
        DOC_CACHE.set(key, payload)
    return [_as_parent(p) for p in payload["parents"]]


def _parent_cache_key(doc_id: str, ingest_version: str) -> str:
    return f"{doc_id}@{ingest_version}"


def load_children(doc_id: str, tenant_id: str) -> list[ChildChunk]:
    with session() as sess:
        stmt = (
            select(Chunk)
            .where(Chunk.tenant_id == tenant_id, Chunk.doc_id == doc_id)
            .order_by(Chunk.ordinal)
        )
        return [_as_child(c) for c in sess.scalars(stmt)]


def _document(doc_id: str, tenant_id: str) -> Optional[Document]:
    with session() as sess:
        stmt = select(Document).where(
            Document.doc_id == doc_id, Document.tenant_id == tenant_id
        )
        return sess.scalars(stmt).first()


def _live_document(doc_id: str, tenant_id: str) -> Optional[Document]:
    with session() as sess:
        stmt = select(Document).where(_listable(tenant_id), Document.doc_id == doc_id)
        return sess.scalars(stmt).first()


def _find_by_filename(filename: str, new_doc_id: str, tenant_id: str) -> Optional[str]:
    """The live document this upload replaces, if any. A superseded or deleted row with the same
    name is history, not something to replace again."""
    with session() as sess:
        stmt = select(Document.doc_id).where(
            Document.tenant_id == tenant_id,
            Document.filename == filename,
            Document.doc_id != new_doc_id,
            Document.deleted_at.is_(None),
            Document.superseded_by.is_(None),
        )
        return sess.scalars(stmt).first()


def _begin_document(doc_id: str, filename: str, owner: Principal, version: int = 1) -> None:
    """Claim the row before parsing, so a job that dies inside a 200-page parse leaves its state
    behind instead of nothing. The counts are not known yet and are filled in at `parsed`."""
    values = {
        "doc_id": doc_id,
        "tenant_id": owner.tenant_id,
        "owner_id": owner.user_id,
        "filename": filename,
        "ingest_version": SETTINGS.ingest_version,
        "pages": 0,
        "elements_by_kind": {},
        "num_parents": 0,
        "num_children": 0,
        "state": PENDING,
        "version": version,
        # A re-upload of something deleted is an undelete, not a second document: the id is the
        # content hash, so the row that comes back is the row that was there.
        "deleted_at": None,
        "superseded_by": None,
    }
    with session() as sess:
        stmt = insert(Document).values(values)
        sess.execute(
            stmt.on_conflict_do_update(
                index_elements=[Document.doc_id],
                set_={
                    "state": PENDING,
                    "version": version,
                    "deleted_at": None,
                    "superseded_by": None,
                    "ingest_version": SETTINGS.ingest_version,
                },
            )
        )


def _upsert_document(report: IngestReport, owner: Principal, state: State) -> None:
    values = {
        "doc_id": report.doc_id,
        "tenant_id": owner.tenant_id,
        "owner_id": owner.user_id,
        "filename": report.filename,
        "ingest_version": SETTINGS.ingest_version,
        "pages": report.pages,
        "elements_by_kind": report.elements_by_kind,
        "num_parents": report.num_parents,
        "num_children": report.num_children,
        "state": state,
        "deleted_at": None,
        "superseded_by": None,
    }
    with session() as sess:
        stmt = insert(Document).values(values)
        sess.execute(
            stmt.on_conflict_do_update(
                index_elements=[Document.doc_id],
                set_={k: stmt.excluded[k] for k in values if k != "doc_id"},
            )
        )


def _purge(doc_id: str) -> None:
    """Hard delete: rows, chunks (by cascade) and every stored byte. The end of the retention
    window, or an erasure request -- never the ordinary delete path."""
    with session() as sess:
        sess.execute(sa_delete(Document).where(Document.doc_id == doc_id))
    objects.delete_doc(doc_id)


def _supersede(old_doc_id: str, new_doc_id: str) -> None:
    with session() as sess:
        sess.execute(
            update(Document)
            .where(Document.doc_id == old_doc_id)
            .values(superseded_by=new_doc_id)
        )
        sess.execute(sa_delete(Chunk).where(Chunk.doc_id == old_doc_id))


def _next_version(replaced: Optional[str]) -> int:
    if replaced is None:
        return 1
    with session() as sess:
        current = sess.scalar(select(Document.version).where(Document.doc_id == replaced))
    return (current or 0) + 1


def _listable(tenant_id: str) -> ColumnElement[bool]:
    """What a reader may see: this tenant's live documents, neither deleted nor replaced, that
    are actually indexed at the build the alias points at. One predicate, so listing and scoping
    cannot drift apart.

    The last clause is the one worth explaining. It was originally a comparison against the
    document row's own `ingest_version`, and that is subtly wrong in both directions: a document
    rebuilt at a new build would vanish the moment the row was updated and before the cutover,
    and a rollback would hide everything the rebuild had touched even though the old chunks were
    still there. Presence at the active build is the honest test, because it is the same thing
    retrieval asks -- a document is listed exactly when it can answer.
    """
    return and_(
        Document.tenant_id == tenant_id,
        Document.state == LIVE,
        Document.deleted_at.is_(None),
        Document.superseded_by.is_(None),
        Document.doc_id.in_(
            select(Chunk.doc_id).where(
                Chunk.tenant_id == tenant_id, Chunk.ingest_version == alias.active_version()
            )
        ),
    )


def _as_parent(data: dict) -> ParentChunk:
    data = {**data, "section_path": tuple(data["section_path"])}
    return ParentChunk(**data)


def _as_child(row: Chunk) -> ChildChunk:
    return ChildChunk(
        chunk_id=row.chunk_id,
        parent_id=row.parent_id,
        doc_id=row.doc_id,
        filename=row.filename,
        text=row.text,
        embed_text=row.embed_text,
        page_start=row.page_start,
        page_end=row.page_end,
        section_path=tuple(row.section_path),
        kind=row.kind,
        char_span_in_parent=tuple(row.char_span_in_parent),
    )


def _page_count(elements: list[Element]) -> int:
    return max((e.page for e in elements), default=0)


def _emit(on_stage: Optional[OnStage], label: str) -> None:
    if on_stage is not None:
        on_stage(label)


# --- lifecycle sweeps and reindex support ---


def stale_documents(limit: Optional[int] = None) -> list[dict]:
    """Documents that are not built at the version the running configuration would produce.

    This is the reindex worklist. It deliberately ignores the alias: the question is not what
    is being read, it is what still has to be rebuilt before the alias can move.
    """
    with session() as sess:
        # Absence of chunks at the target build, not a mismatched row: that makes the worklist
        # a description of what is left to do, so a rebuild interrupted half way resumes rather
        # than restarting.
        built = select(Chunk.doc_id).where(Chunk.ingest_version == SETTINGS.ingest_version)
        stmt = (
            select(Document)
            .where(
                Document.state == LIVE,
                Document.deleted_at.is_(None),
                Document.superseded_by.is_(None),
                Document.doc_id.not_in(built),
            )
            .order_by(Document.created_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            {
                "doc_id": row.doc_id,
                "tenant_id": row.tenant_id,
                "owner_id": row.owner_id,
                "filename": row.filename,
                "from_version": row.ingest_version,
            }
            for row in sess.scalars(stmt)
        ]


def reindex_document(
    doc_id: str,
    filename: str,
    owner: Principal,
    embedder: Embedding,
    captioner: Optional[FigureCaptioner] = None,
) -> IngestReport:
    """Rebuild one document at the running configuration's version, beside its existing index.

    The old chunks are left alone, which is what lets the alias keep serving them while this
    runs. Nothing here is visible to a reader until `alias.promote` moves the pointer.
    """
    data = objects.get_raw(doc_id, filename)
    extension = validate_upload(filename, data)
    elements = parse(extension, data, captioner)
    build = SETTINGS.ingest_version
    parents, children = chunk_document(elements, doc_id, filename, build)
    objects.put_parents(
        doc_id, build, json.dumps({"parents": [asdict(p) for p in parents]}).encode()
    )
    DOC_CACHE.delete(_parent_cache_key(doc_id, build))
    embeddings = embedder.encode([c.embed_text for c in children])
    with session() as sess:
        VectorStore().add(children, embeddings, owner.tenant_id, sess=sess, ingest_version=build)
        sess.execute(
            update(Document)
            .where(Document.doc_id == doc_id)
            .values(
                ingest_version=build,
                pages=_page_count(elements),
                elements_by_kind=dict(Counter(e.kind for e in elements)),
                num_parents=len(parents),
                num_children=len(children),
                state=LIVE,
            )
        )
    return IngestReport(
        doc_id=doc_id,
        filename=filename,
        pages=_page_count(elements),
        elements_by_kind=dict(Counter(e.kind for e in elements)),
        num_parents=len(parents),
        num_children=len(children),
        outcome="indexed",
    )


def sweep_expired() -> dict[str, int]:
    """Hard-delete what has outlived its retention window. Idempotent: a missed run costs
    nothing, and running it twice removes the same nothing the second time."""
    retention = SETTINGS.retention
    now = datetime.now(timezone.utc)
    purged: list[str] = []
    with session() as sess:
        conditions = []
        if retention.soft_delete_days:
            conditions.append(
                Document.deleted_at < now - timedelta(days=retention.soft_delete_days)
            )
        if retention.superseded_days:
            conditions.append(
                and_(
                    Document.superseded_by.isnot(None),
                    Document.created_at < now - timedelta(days=retention.superseded_days),
                )
            )
        if conditions:
            purged = list(sess.scalars(select(Document.doc_id).where(or_(*conditions))))
    for doc_id in purged:
        _purge(doc_id)
    return {
        "documents_purged": len(purged),
        "chunks_purged": _sweep_old_index(now),
        "abandoned_purged": _sweep_abandoned(now),
    }


def _sweep_abandoned(now: datetime) -> int:
    """Collect rows left by an ingest whose process was killed rather than failing.

    `discard_failed` runs in the failure path, so a job that raises cleans up after itself. A
    job that is `SIGKILL`ed -- an evicted pod, an OOM -- does not, and leaves a row short of
    `live` that nothing was sweeping. The cutoff is twice the job timeout: past that, a job
    that was still running would have been killed by its own timeout anyway, so a non-live row
    that old is abandoned rather than in progress.
    """
    cutoff = now - timedelta(seconds=SETTINGS.jobs.job_timeout_s * 2)
    with session() as sess:
        abandoned = list(
            sess.scalars(
                select(Document.doc_id).where(
                    Document.state != LIVE, Document.created_at < cutoff
                )
            )
        )
    for doc_id in abandoned:
        discard_failed(doc_id)
    return len(abandoned)


def _sweep_old_index(now: datetime) -> int:
    """Drop the chunks of builds the alias no longer points at, once the rollback window has
    passed. Until then they *are* the rollback, so the clock runs from the cutover -- not from
    when the documents were uploaded, which has nothing to do with it.
    """
    days = SETTINGS.retention.old_index_days
    if not days:
        return 0
    state = alias.state()
    cutover = state["updated_at"]
    if cutover is None or datetime.fromisoformat(cutover) > now - timedelta(days=days):
        return 0
    with session() as sess:
        stale = list(
            sess.scalars(
                select(Chunk.chunk_id).where(Chunk.ingest_version != state["active_version"])
            )
        )
        if stale:
            sess.execute(sa_delete(Chunk).where(Chunk.chunk_id.in_(stale)))
        return len(stale)


def pending_work() -> dict[str, int]:
    """Two numbers the scrape exports, and the only honest way to alert on either job.

    "The reindex has not run" and "nothing needed reindexing" produce the same counter delta,
    and so do "the sweep is broken" and "nobody deleted anything". A backlog does not: a
    document that is still unbuilt at the target version, or still present past its window, is
    work that is owed regardless of what has or has not been running.
    """
    now = datetime.now(timezone.utc)
    retention = SETTINGS.retention
    with session() as sess:
        built = select(Chunk.doc_id).where(Chunk.ingest_version == SETTINGS.ingest_version)
        stale = sess.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.state == LIVE,
                Document.deleted_at.is_(None),
                Document.superseded_by.is_(None),
                Document.doc_id.not_in(built),
            )
        )
        overdue: Optional[int] = 0
        if retention.enabled and retention.soft_delete_days:
            overdue = sess.scalar(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.deleted_at
                    < now - timedelta(days=retention.soft_delete_days)
                )
            )
    return {"reindex_pending": int(stale or 0), "retention_overdue": int(overdue or 0)}
