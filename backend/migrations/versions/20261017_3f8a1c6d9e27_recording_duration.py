from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f8a1c6d9e27"
down_revision: str | None = "5b1d7e93c2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("duration_seconds", sa.Double(), nullable=True))


def downgrade() -> None:
    recordings = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM documents WHERE duration_seconds IS NOT NULL")
    )
    if recordings:
        message = (
            f"{recordings} uploaded recordings are stored, which the previous revision cannot read; wait for "
            "document retention to purge them, or delete them deliberately, then downgrade"
        )
        raise RuntimeError(message)
    op.drop_column("documents", "duration_seconds")
