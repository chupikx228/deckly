from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c4e2f7a1b93"
down_revision: str | None = "3f8a1c6d9e27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_url", sa.Text(), nullable=True))


def downgrade() -> None:
    articles = op.get_bind().scalar(sa.text("SELECT count(*) FROM documents WHERE source_url IS NOT NULL"))
    if articles:
        message = (
            f"{articles} pages fetched from a url are stored, which the previous revision cannot read; "
            "wait for document retention to purge them, or delete them deliberately, then downgrade"
        )
        raise RuntimeError(message)
    op.drop_column("documents", "source_url")
