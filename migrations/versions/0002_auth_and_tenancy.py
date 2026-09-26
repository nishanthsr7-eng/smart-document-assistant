"""tenants, users, audit log, and tenant scoping on documents and chunks

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "users",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(36),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "audit_log",
        sa.Column("event_id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("doc_id", sa.String(64), nullable=True),
        sa.Column("detail", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_log_tenant_created", "audit_log", ["tenant_id", "created_at"])

    # doc_id is now sha256(tenant_id + content), so pre-tenancy rows cannot be assigned an owner
    # without inventing one. There is no production data behind this revision: drop and re-ingest.
    op.execute("DELETE FROM documents")

    op.add_column("documents", sa.Column("tenant_id", sa.String(36), nullable=False))
    op.add_column("documents", sa.Column("owner_id", sa.String(36), nullable=False))
    op.create_foreign_key(
        "fk_documents_tenant_id",
        "documents",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_documents_filename", table_name="documents")
    op.create_index("ix_documents_tenant_filename", "documents", ["tenant_id", "filename"])

    op.add_column("chunks", sa.Column("tenant_id", sa.String(36), nullable=False))
    op.create_index("ix_chunks_tenant_id", "chunks", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_chunks_tenant_id", table_name="chunks")
    op.drop_column("chunks", "tenant_id")

    op.drop_index("ix_documents_tenant_filename", table_name="documents")
    op.create_index("ix_documents_filename", "documents", ["filename"])
    op.drop_constraint("fk_documents_tenant_id", "documents", type_="foreignkey")
    op.drop_column("documents", "owner_id")
    op.drop_column("documents", "tenant_id")

    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
