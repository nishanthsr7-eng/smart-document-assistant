import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Literal, Optional, Protocol

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from src.core.cache import ANSWER_CACHE, DOC_CACHE
from src.core.config import SETTINGS
from src.core.tracing import OnStage
from src.ingestion.chunker import ChildChunk, ParentChunk, chunk_document
from src.ingestion.parsers import Element, FigureCaptioner, parse, validate_upload
from src.retrieval.vector_store import VectorStore
from src.storage import objects
from src.storage.db import session
from src.storage.models import Chunk, Document
from src.storage.redis_client import lock

Outcome = Literal["indexed", "replaced", "duplicate"]


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
    captioner: Optional[FigureCaptioner] = None,
    on_stage: Optional[OnStage] = None,
) -> IngestReport:
    """Parse, chunk, embed and index one document as a single atomic unit.

    Embedding runs inside the lock: releasing it before the index write let a concurrent upload
    of the same filename purge the document row while this one was still embedding, and the
    chunk insert then failed on the foreign key.
    """
    extension = validate_upload(filename, data)
    doc_id = hashlib.sha256(data).hexdigest()

    # Serialized across workers: the replace-by-filename path is a read-modify-write on the
    # document set, and two concurrent uploads of the same name would otherwise interleave.
    with lock(f"ingest:{filename}", SETTINGS.storage.ingest_lock_ttl_s):
        existing = _live_document(doc_id)
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

        replaced = _find_by_filename(filename, doc_id)

        _emit(on_stage, "Parsing document")
        elements = parse(extension, data, captioner)

        figures = sum(1 for e in elements if e.kind == "figure")
        if figures and captioner is not None:
            _emit(on_stage, f"Captioning {figures} figure(s)")

        _emit(on_stage, "Chunking")
        parents, children = chunk_document(elements, doc_id, filename)

        _emit(on_stage, f"Indexing {len(children)} chunks")
        if replaced:
            _purge(replaced)

        objects.put_raw(doc_id, filename, data)
        objects.put_parents(doc_id, json.dumps({"parents": [asdict(p) for p in parents]}).encode())
        DOC_CACHE.delete(doc_id)

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
        _upsert_document(report)

        _emit(on_stage, f"Embedding {len(children)} chunks")
        embeddings = embedder.encode([c.embed_text for c in children])
        VectorStore().add(children, embeddings)
        _mark_live(doc_id)

    ANSWER_CACHE.clear()
    return report


def _mark_live(doc_id: str) -> None:
    with session() as sess:
        sess.execute(update(Document).where(Document.doc_id == doc_id).values(state="live"))


def list_indexed() -> list[dict]:
    with session() as sess:
        stmt = (
            select(Document)
            .where(
                Document.ingest_version == SETTINGS.ingest_version,
                Document.state == "live",
            )
            .order_by(Document.created_at)
        )
        return [
            {
                "doc_id": doc.doc_id,
                "filename": doc.filename,
                "pages": doc.pages,
                "num_children": doc.num_children,
            }
            for doc in sess.scalars(stmt)
        ]


def delete(doc_id: str) -> None:
    _purge(doc_id)
    ANSWER_CACHE.clear()


def load_parents(doc_id: str) -> list[ParentChunk]:
    payload = DOC_CACHE.get(doc_id)
    if payload is None:
        payload = json.loads(objects.get_parents(doc_id))
        DOC_CACHE.set(doc_id, payload)
    return [_as_parent(p) for p in payload["parents"]]


def load_children(doc_id: str) -> list[ChildChunk]:
    with session() as sess:
        stmt = select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.ordinal)
        return [_as_child(c) for c in sess.scalars(stmt)]


def _live_document(doc_id: str) -> Optional[Document]:
    with session() as sess:
        stmt = select(Document).where(
            Document.doc_id == doc_id,
            Document.ingest_version == SETTINGS.ingest_version,
            Document.state == "live",
        )
        return sess.scalars(stmt).first()


def _find_by_filename(filename: str, new_doc_id: str) -> Optional[str]:
    with session() as sess:
        stmt = select(Document.doc_id).where(
            Document.filename == filename, Document.doc_id != new_doc_id
        )
        return sess.scalars(stmt).first()


def _upsert_document(report: IngestReport) -> None:
    values = {
        "doc_id": report.doc_id,
        "filename": report.filename,
        "ingest_version": SETTINGS.ingest_version,
        "pages": report.pages,
        "elements_by_kind": report.elements_by_kind,
        "num_parents": report.num_parents,
        "num_children": report.num_children,
        "state": "pending",
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
    with session() as sess:
        sess.execute(sa_delete(Document).where(Document.doc_id == doc_id))
    objects.delete_doc(doc_id)
    DOC_CACHE.delete(doc_id)


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
