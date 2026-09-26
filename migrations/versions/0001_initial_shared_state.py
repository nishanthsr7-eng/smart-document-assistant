"""documents + chunks with pgvector embeddings and a tsvector lexical index

Revision ID: 0001
Revises:
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from src.core.config import SETTINGS

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("doc_id", sa.String(64), primary_key=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("ingest_version", sa.String(32), nullable=False),
        sa.Column("pages", sa.Integer, nullable=False),
        sa.Column("elements_by_kind", JSONB, nullable=False),
        sa.Column("num_parents", sa.Integer, nullable=False),
        sa.Column("num_children", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_documents_filename", "documents", ["filename"])

    op.create_table(
        "chunks",
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column(
            "doc_id",
            sa.String(64),
            sa.ForeignKey("documents.doc_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embed_text", sa.Text, nullable=False),
        sa.Column("lexical_text", sa.Text, nullable=False),
        sa.Column("page_start", sa.Integer, nullable=False),
        sa.Column("page_end", sa.Integer, nullable=False),
        sa.Column("section_path", JSONB, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("char_span_in_parent", JSONB, nullable=False),
        sa.Column("embedding", Vector(SETTINGS.storage.embedding_dim), nullable=True),
        sa.Column(
            "tsv",
            TSVECTOR,
            sa.Computed("to_tsvector('english', lexical_text)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index("ix_chunks_doc_id", "chunks", ["doc_id"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_chunks_embedding",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("documents")
