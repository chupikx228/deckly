import asyncio
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name, job_key_prefix
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import IdempotencyScope
from deckly.config import Settings, load_settings
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.queue import create_queue_pool, create_redis_settings
from deckly.infrastructure.tables import GenerationJobRow


def redis_settings(settings: Settings) -> RedisSettings:
    return create_redis_settings(
        str(settings.redis.url),
        connect_timeout_seconds=settings.redis.connect_timeout_seconds,
        connect_retries=settings.redis.connect_retries,
    )


class Cleanup:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.client_ids: set[UUID] = set()
        self.job_ids: set[UUID] = set()

    def scope(self) -> IdempotencyScope:
        client_id = uuid4()
        self.client_ids.add(client_id)
        return IdempotencyScope(client_id=client_id, idempotency_key=uuid4())

    async def purge(self) -> None:
        engine = create_engine(
            str(self._settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10
        )
        try:
            async with create_session_factory(engine).begin() as session:
                await session.execute(
                    delete(GenerationJobRow).where(GenerationJobRow.client_id.in_(self.client_ids))
                )
        finally:
            await engine.dispose()
        pool = await create_queue_pool(redis_settings(self._settings))
        try:
            for job_id in self.job_ids:
                await pool.delete(f"{job_key_prefix}{job_id}")
                await pool.zrem(default_queue_name, str(job_id))
        finally:
            await pool.aclose()


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def cleanup(settings: Settings) -> Iterator[Cleanup]:
    tracker = Cleanup(settings)
    yield tracker
    asyncio.run(tracker.purge())


@pytest.fixture
async def session_factory(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout_seconds=settings.database.pool_timeout_seconds,
    )
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def queue_pool(settings: Settings) -> AsyncIterator[ArqRedis]:
    pool = await create_queue_pool(redis_settings(settings))
    try:
        yield pool
    finally:
        await pool.aclose()
