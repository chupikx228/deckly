from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "429d9692d6b6"
down_revision: str | None = "4970fd4be2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("generation_jobs", "failure_reason", new_column_name="failure_code")
    op.add_column("generation_jobs", sa.Column("result", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("generation_jobs", "result")
    op.alter_column("generation_jobs", "failure_code", new_column_name="failure_reason")
