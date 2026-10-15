from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "be09cbffc437"
down_revision: str | None = "8e41c3a95d27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_documents")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_documents_storage_key")),
    )
    op.create_index(
        "ix_documents_unused_uploaded_at",
        "documents",
        ["uploaded_at"],
        unique=False,
        postgresql_where=sa.text("used_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_unused_uploaded_at", table_name="documents")
    op.drop_table("documents")
