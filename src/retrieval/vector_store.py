from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.core.config import SETTINGS
from src.ingestion.chunker import ChildChunk, build_header
from src.storage import alias
from src.storage.db import session
from src.storage.models import Chunk


@dataclass
class Hit:
    chunk_id: str
    parent_id: str
    doc_id: str
    filename: str
    text: str
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    kind: str
    char_span_in_parent: tuple[int, int]
    score: float


class VectorStore:
    """pgvector-backed. Stateless handle: all state lives in Postgres, shared by every worker.

    Every method takes tenant_id and applies it as a predicate. The filter lives here rather
    than in callers so no call site can forget it: an omitted argument is a type error.
    """

    def add(
        self,
        children: list[ChildChunk],
        embeddings: list[list[float]],
        tenant_id: str,
        sess: Optional[Session] = None,
        ingest_version: Optional[str] = None,
    ) -> None:
        """Pass `sess` to enrol the index write in the caller's transaction.

        Ingest does: the document row that claims these chunks and the chunks themselves must
        commit together, or a crash between the two leaves a document asserting an index that
        is not there.
        """
        if not children:
            return
        version = ingest_version or SETTINGS.ingest_version
        rows = [
            {**_row(child, i, tenant_id, version), "embedding": embedding}
            for i, (child, embedding) in enumerate(zip(children, embeddings, strict=True))
        ]
        stmt = insert(Chunk).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Chunk.chunk_id],
            set_={
                "embedding": stmt.excluded.embedding,
                "text": stmt.excluded.text,
                # Re-writing a chunk re-dates it to the build that wrote it. Ids normally
                # differ between builds, so this only fires when the same build writes twice --
                # a retried ingest -- and then the version it claims must be the current one.
                "ingest_version": stmt.excluded.ingest_version,
            },
        )
        if sess is not None:
            sess.execute(stmt)
            return
        with session() as own:
            own.execute(stmt)

    def query(self, vector: list[float], k: int, doc_ids: list[str], tenant_id: str) -> list[Hit]:
        if not doc_ids:
            return []
        # Pinned to the alias, not to the running configuration: during a reindex both builds'
        # chunks are in this table, and a read must see exactly one of them.
        version = alias.active_version()
        distance = Chunk.embedding.cosine_distance(vector)
        with session() as sess:
            stmt = (
                select(Chunk, distance.label("distance"))
                .where(
                    Chunk.tenant_id == tenant_id,
                    Chunk.ingest_version == version,
                    Chunk.doc_id.in_(doc_ids),
                    Chunk.embedding.isnot(None),
                )
                .order_by(distance)
                .limit(k)
            )
            return [to_hit(chunk, 1.0 - float(dist)) for chunk, dist in sess.execute(stmt)]

    def get_embeddings(self, chunk_ids: list[str], tenant_id: str) -> dict[str, list[float]]:
        if not chunk_ids:
            return {}
        with session() as sess:
            stmt = select(Chunk.chunk_id, Chunk.embedding).where(
                Chunk.tenant_id == tenant_id, Chunk.chunk_id.in_(chunk_ids)
            )
            return {cid: list(vec) for cid, vec in sess.execute(stmt) if vec is not None}


def _row(c: ChildChunk, ordinal: int, tenant_id: str, ingest_version: str) -> dict:
    return {
        "chunk_id": c.chunk_id,
        "doc_id": c.doc_id,
        "tenant_id": tenant_id,
        "ingest_version": ingest_version,
        "parent_id": c.parent_id,
        "ordinal": ordinal,
        "filename": c.filename,
        "text": c.text,
        "embed_text": c.embed_text,
        "lexical_text": f"{build_header(c.filename, c.section_path)}\n{c.text}",
        "page_start": c.page_start,
        "page_end": c.page_end,
        "section_path": list(c.section_path),
        "kind": c.kind,
        "char_span_in_parent": list(c.char_span_in_parent),
        "embedding": None,
    }


def to_hit(chunk: Chunk, score: float) -> Hit:
    return Hit(
        chunk_id=chunk.chunk_id,
        parent_id=chunk.parent_id,
        doc_id=chunk.doc_id,
        filename=chunk.filename,
        text=chunk.text,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        section_path=tuple(chunk.section_path),
        kind=chunk.kind,
        char_span_in_parent=tuple(chunk.char_span_in_parent),
        score=score,
    )
