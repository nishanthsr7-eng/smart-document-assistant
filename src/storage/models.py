from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
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


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    # Email is globally unique: a login carries no tenant, so it must identify one user.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_users_tenant_id", "tenant_id"),)


class AuditEvent(Base):
    __tablename__ = "audit_log"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),)


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    ingest_version: Mapped[str] = mapped_column(String(32), nullable=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False)
    elements_by_kind: Mapped[dict] = mapped_column(JSONB, nullable=False)
    num_parents: Mapped[int] = mapped_column(Integer, nullable=False)
    num_children: Mapped[int] = mapped_column(Integer, nullable=False)
    # "pending" until the embedding write is acknowledged; only "live" docs are listed or queried.
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Soft delete: the row and the stored bytes outlive the index by the purge window, so a
    # delete made in error is recoverable. The chunks go immediately -- a deleted document must
    # stop being answerable the moment it is deleted, whatever the window says about the bytes.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Replacing a file by name no longer destroys what it replaced: the old row points at the
    # new one and is retained for the same window, which is what makes "which version answered
    # that question last month" a question with an answer.
    superseded_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_documents_tenant_filename", "tenant_id", "filename"),
        # The listing predicate, which every query and every page of /documents runs.
        Index("ix_documents_listing", "tenant_id", "ingest_version", "state"),
        Index("ix_documents_deleted_at", "deleted_at"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized from documents so the retrieval filter is a predicate, not a join.
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # Which build of the chunker and embedder produced this row. Two builds of the same document
    # coexist during a reindex, and reads are pinned to whichever one the alias points at.
    ingest_version: Mapped[str] = mapped_column(String(32), nullable=False)
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
        Index("ix_chunks_tenant_id", "tenant_id"),
        Index("ix_chunks_tenant_version", "tenant_id", "ingest_version"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Webhook(Base):
    """A tenant's subscription to ingest outcomes. The secret signs the delivery, so a receiver
    can tell our POST from anyone else's; it is write-only over the API."""

    __tablename__ = "webhooks"

    webhook_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Flipped off after `webhook_max_failures` consecutive failures: an endpoint that has been
    # gone for a week should stop costing every ingest a timeout.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    last_delivery_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_webhooks_tenant_id", "tenant_id"),)



class IndexAlias(Base):
    """Which build of the index reads are served from.

    The blue-green pointer. A reindex writes a whole new set of chunks under a new
    `ingest_version` while this still names the old one, so every read during the rebuild is
    served by the index that is known to work. The cutover is this one row changing, and the
    rollback is changing it back -- the old chunks are still there until they are purged.
    """

    __tablename__ = "index_alias"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    ingest_version: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
