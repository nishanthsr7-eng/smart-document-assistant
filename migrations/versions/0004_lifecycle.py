"""soft delete, document versions, and the blue-green index alias

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("superseded_by", sa.String(64), nullable=True))
    op.add_column(
        "documents", sa.Column("version", sa.Integer, nullable=False, server_default="1")
    )
    op.create_index("ix_documents_listing", "documents", ["tenant_id", "ingest_version", "state"])
    op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])

    # Existing chunks were written by whatever build produced their document, so that is where
    # the value comes from; the column is only NOT NULL once every row has one.
    op.add_column("chunks", sa.Column("ingest_version", sa.String(32), nullable=True))
    op.execute(
        "UPDATE chunks SET ingest_version = documents.ingest_version "
        "FROM documents WHERE chunks.doc_id = documents.doc_id"
    )
    op.execute("DELETE FROM chunks WHERE ingest_version IS NULL")
    op.alter_column("chunks", "ingest_version", nullable=False)
    op.create_index("ix_chunks_tenant_version", "chunks", ["tenant_id", "ingest_version"])

    op.create_table(
        "index_alias",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("ingest_version", sa.String(32), nullable=False),
        sa.Column("previous_version", sa.String(32), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Seeded from what is already indexed, so an existing deployment keeps serving the corpus it
    # has rather than an empty one. A fresh install has no documents and is seeded on first use.
    op.execute(
        "INSERT INTO index_alias (name, ingest_version) "
        "SELECT 'active', ingest_version FROM documents "
        "WHERE state = 'live' GROUP BY ingest_version ORDER BY count(*) DESC LIMIT 1"
    )


def downgrade() -> None:
    op.drop_table("index_alias")
    op.drop_index("ix_chunks_tenant_version", table_name="chunks")
    op.drop_column("chunks", "ingest_version")
    op.drop_index("ix_documents_deleted_at", table_name="documents")
    op.drop_index("ix_documents_listing", table_name="documents")
    op.drop_column("documents", "version")
    op.drop_column("documents", "superseded_by")
    op.drop_column("documents", "deleted_at")
