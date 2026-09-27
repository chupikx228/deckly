import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis, RedisSettings
from arq.constants import abort_jobs_ss, default_queue_name, job_key_prefix
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import IdempotencyScope
from deckly.config import Settings, load_settings
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.queue import ArqJobQueue, create_queue_pool, create_redis_settings
from deckly.infrastructure.quota import ADDRESS_KEY_PREFIX, CLIENT_KEY_PREFIX, address_bucket
from deckly.infrastructure.tables import GenerationJobRow

COMMAND_TIMEOUT_SECONDS = 2
TEST_PEER_ADDRESSES = ("testclient", "127.0.0.1")


def redis_settings(settings: Settings) -> RedisSettings:
    return create_redis_settings(
        str(settings.redis.url),
        connect_timeout_seconds=settings.redis.connect_timeout_seconds,
        connect_retries=settings.redis.connect_retries,
    )


def job_queue(pool: ArqRedis) -> ArqJobQueue:
    return ArqJobQueue(pool, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS)


async def open_queue_pool(settings: Settings) -> ArqRedis:
    return await create_queue_pool(
        redis_settings(settings), read_timeout_seconds=settings.redis.connect_timeout_seconds
    )


def with_generation_limits(settings: Settings, *, per_client: int, per_address: int) -> Settings:
    return replace(
        settings,
        limits=settings.limits.model_copy(
            update={"generation_jobs_per_day": per_client, "generation_jobs_per_address_per_day": per_address}
        ),
    )


async def purge_quota(pool: ArqRedis, client_ids: Iterable[UUID], addresses: Iterable[str]) -> None:
    patterns = [f"{CLIENT_KEY_PREFIX}:{client_id}:*" for client_id in client_ids] + [
        f"{ADDRESS_KEY_PREFIX}:{address_bucket(address)}:*" for address in addresses
    ]
    for pattern in patterns:
        async for key in pool.scan_iter(match=pattern):
            await pool.delete(key)


class Cleanup:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.client_ids: set[UUID] = set()
        self.job_ids: set[UUID] = set()
        self.addresses: set[str] = set(TEST_PEER_ADDRESSES)

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
        pool = await open_queue_pool(self._settings)
        try:
            for job_id in self.job_ids:
                await pool.delete(f"{job_key_prefix}{job_id}")
                await pool.zrem(default_queue_name, str(job_id))
                await pool.zrem(abort_jobs_ss, str(job_id))
            await purge_quota(pool, self.client_ids, self.addresses)
        finally:
            await pool.aclose()


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def restored_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield
    finally:
        root.handlers, root.level = handlers, level


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
    pool = await open_queue_pool(settings)
    try:
        yield pool
    finally:
        await pool.aclose()
