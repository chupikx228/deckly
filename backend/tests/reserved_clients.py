from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Text, cast, delete

from deckly.config import Settings
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.tables import GenerationJobRow

RESERVED_NAMESPACE = "7e57-47e5-8de1"
RESERVED_PATTERN = f"________-{RESERVED_NAMESPACE}-____________"
ABANDONED_AFTER = timedelta(hours=1)


def reserved_client_id(minted_at: datetime | None = None) -> UUID:
    moment = utc_now() if minted_at is None else minted_at
    return UUID(f"{int(moment.timestamp()):08x}-{RESERVED_NAMESPACE}-{uuid4().hex[:12]}")


def minted_at_or_after(moment: datetime) -> UUID:
    return UUID(f"{int(moment.timestamp()):08x}-0000-0000-0000-000000000000")


async def purge_abandoned_test_jobs(settings: Settings, now: datetime) -> int:
    statement = (
        delete(GenerationJobRow)
        .where(
            cast(GenerationJobRow.client_id, Text).like(RESERVED_PATTERN),
            GenerationJobRow.client_id < minted_at_or_after(now - ABANDONED_AFTER),
        )
        .returning(GenerationJobRow.job_id)
    )
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        async with create_session_factory(engine).begin() as session:
            return len((await session.scalars(statement)).all())
    finally:
        await engine.dispose()
