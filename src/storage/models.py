from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.config import SETTINGS


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    ingest_version: Mapped[str] = mapped_column(String(32), nullable=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False)
    elements_by_kind: Mapped[dict] = mapped_column(JSONB, nullable=False)
    num_parents: Mapped[int] = mapped_column(Integer, nullable=False)
    num_children: Mapped[int] = mapped_column(Integer, nullable=False)
    # "pending" until the embedding write is acknowledged; only "live" docs are listed or queried.
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_documents_filename", "filename"),)


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embed_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Header + text, i.e. what BM25 used to tokenize; the tsvector is derived from it.
    lexical_text: Mapped[str] = mapped_column(Text, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[list] = mapped_column(JSONB, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    char_span_in_parent: Mapped[list] = mapped_column(JSONB, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(SETTINGS.storage.embedding_dim), nullable=True
    )
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', lexical_text)", persisted=True),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_chunks_doc_id", "doc_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
