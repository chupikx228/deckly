from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b1d7e93c2a4"
down_revision: str | None = "be09cbffc437"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generation_jobs", sa.Column("document_id", sa.Uuid(), nullable=True))
    op.alter_column("generation_jobs", "topic", existing_type=sa.Text(), nullable=True)
    op.create_foreign_key(
        op.f("fk_generation_jobs_document_id_documents"),
        "generation_jobs",
        "documents",
        ["document_id"],
        ["document_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_generation_jobs_one_source"), "generation_jobs", "num_nonnulls(topic, document_id) = 1"
    )
    op.create_index(
        "ix_generation_jobs_document_id",
        "generation_jobs",
        ["document_id"],
        unique=False,
        postgresql_where=sa.text("document_id IS NOT NULL"),
    )
    op.drop_index("ix_documents_unused_uploaded_at", table_name="documents")
    op.create_index("ix_documents_uploaded_at", "documents", ["uploaded_at"], unique=False)


def downgrade() -> None:
    referencing = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM generation_jobs WHERE document_id IS NOT NULL")
    )
    if referencing:
        message = (
            f"{referencing} generation jobs generate from an uploaded document, which the previous revision "
            "cannot store; wait for job retention to purge them, or delete them deliberately, then downgrade"
        )
        raise RuntimeError(message)
    op.drop_index("ix_documents_uploaded_at", table_name="documents")
    op.create_index(
        "ix_documents_unused_uploaded_at",
        "documents",
        ["uploaded_at"],
        unique=False,
        postgresql_where=sa.text("used_at IS NULL"),
    )
    op.drop_index("ix_generation_jobs_document_id", table_name="generation_jobs")
    op.drop_constraint(op.f("ck_generation_jobs_one_source"), "generation_jobs", type_="check")
    op.drop_constraint(
        op.f("fk_generation_jobs_document_id_documents"), "generation_jobs", type_="foreignkey"
    )
    op.alter_column("generation_jobs", "topic", existing_type=sa.Text(), nullable=False)
    op.drop_column("generation_jobs", "document_id")
