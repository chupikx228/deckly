from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.sql.dml import ReturningDelete, ReturningUpdate

from deckly.infrastructure.database import create_engine
from deckly.infrastructure.job_store import purge_finished_statement, release_expired_keys_statement

CUTOFF = datetime(2000, 1, 2, tzinfo=UTC)
POSTGRES_DIALECT = create_engine(
    "postgresql+asyncpg://deckly@localhost/deckly", pool_size=1, max_overflow=0, pool_timeout_seconds=1
).dialect
BATCH_SELECTION = "AS MATERIALIZED"
BATCH_CONSUMER = "IN (SELECT batch.job_id \nFROM batch)"


def postgres_sql(statement: ReturningUpdate[tuple[UUID]] | ReturningDelete[tuple[UUID]]) -> str:
    return str(statement.compile(dialect=POSTGRES_DIALECT))


@pytest.mark.parametrize(
    "statement",
    [release_expired_keys_statement(CUTOFF, 1), purge_finished_statement(CUTOFF, 1)],
    ids=["release_idempotency_keys", "purge_finished"],
)
def test_batch_is_selected_once_so_the_limit_bounds_the_whole_statement(
    statement: ReturningUpdate[tuple[UUID]] | ReturningDelete[tuple[UUID]],
) -> None:
    sql = postgres_sql(statement)

    assert BATCH_SELECTION in sql
    assert BATCH_CONSUMER in sql
    assert "LIMIT" in sql
    assert "SKIP LOCKED" in sql
    assert sql.index("LIMIT") < sql.index(BATCH_CONSUMER)
