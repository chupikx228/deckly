from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e41c3a95d27"
down_revision: str | None = "429d9692d6b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("generation_jobs", "idempotency_key", existing_type=sa.Uuid(), nullable=True)
    op.create_index(
        "ix_generation_jobs_status_updated_at", "generation_jobs", ["status", "updated_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_generation_jobs_status_updated_at", table_name="generation_jobs")
    op.execute(sa.text("DELETE FROM generation_jobs WHERE idempotency_key IS NULL"))
    op.alter_column("generation_jobs", "idempotency_key", existing_type=sa.Uuid(), nullable=False)
