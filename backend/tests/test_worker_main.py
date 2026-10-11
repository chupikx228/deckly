import asyncio
import contextlib
import os
from dataclasses import replace
from pathlib import Path

import pytest
from arq.connections import ArqRedis
from arq.connections import RedisSettings as ArqRedisSettings
from pydantic import RedisDsn
from redis.exceptions import TimeoutError as RedisTimeoutError

from deckly.config import WORKER_MAX_JOBS, RedisSettings, Settings
from deckly.worker import main as worker_main
from deckly.worker.main import build_worker, connect_queue
from deckly.worker.settings import build_cache_redis
from tests.fakes import fresh_observability
from tests.infrastructure.test_queue import (
    OK_REPLY,
    FreezingRedis,
    failure_within_safety_net,
    read_command,
    serving,
)
from tests.test_config import settings_from_environment

pytestmark = pytest.mark.anyio

STARTUP_INFO = b"INFO"
QUEUE_READ = b"ZRANGEBYSCORE"
ABORT_MARKER_COMMIT = b"EXEC"
CACHE_READ = b"GET"
READ_TIMEOUT_SECONDS = 1
NO_RETRIES = 0
EMPTY_ARRAY = b"*0\r\n"
QUEUE_URL = RedisDsn("redis://queue.internal:6380/0")
CACHE_URL = RedisDsn("redis://cache.internal:6381/2")
CACHE_KEY = "deckly:generation-result:v2:probe"


@pytest.fixture
def environment_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    for name in [name for name in os.environ if name.startswith("DECKLY_")]:
        monkeypatch.delenv(name)
    return settings_from_environment(monkeypatch)


class IdleQueueRedis(FreezingRedis):
    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            while command := await read_command(reader):
                name = command[0].upper()
                self.frozen = self.frozen or name == self.freezes_at
                if not self.frozen:
                    writer.write(EMPTY_ARRAY if name == QUEUE_READ else OK_REPLY)
                    await writer.drain()
        writer.close()


def settings_for(base: Settings, redis: ArqRedisSettings) -> Settings:
    return replace(
        base,
        redis=RedisSettings.model_construct(
            url=RedisDsn(f"redis://{redis.host}:{redis.port}/{redis.database}"),
            connect_timeout_seconds=READ_TIMEOUT_SECONDS,
            connect_retries=NO_RETRIES,
        ),
    )


@pytest.mark.parametrize(
    "freezes_at",
    [STARTUP_INFO, QUEUE_READ, ABORT_MARKER_COMMIT],
    ids=["before the first poll", "reading the queue", "delivering abort markers"],
)
async def test_worker_whose_redis_freezes_fails_within_the_read_timeout(
    environment_settings: Settings, freezes_at: bytes
) -> None:
    redis = IdleQueueRedis(freezes_at=freezes_at)
    async with serving(redis) as arq_settings:
        settings = settings_for(environment_settings, arq_settings)
        pool = await connect_queue(settings)
        try:
            worker = build_worker(settings, fresh_observability(), pool)
            worker.on_startup = None
            worker.on_shutdown = None
            failure = await failure_within_safety_net(worker.async_run())
        finally:
            await pool.aclose()

    assert redis.frozen
    assert isinstance(failure, RedisTimeoutError)


async def test_worker_runs_the_pinned_number_of_jobs_at_once(
    environment_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = WORKER_MAX_JOBS + 1
    monkeypatch.setattr(worker_main, "WORKER_MAX_JOBS", pinned)

    worker = build_worker(environment_settings, fresh_observability(), ArqRedis())

    assert worker.max_jobs == pinned


def settings_with_cache(base: Settings, *, queue: RedisDsn, cache: RedisDsn | None) -> Settings:
    return replace(
        base,
        redis=RedisSettings.model_construct(
            url=queue, connect_timeout_seconds=READ_TIMEOUT_SECONDS, connect_retries=NO_RETRIES
        ),
        cache=base.cache.model_copy(update={"redis_url": cache}),
    )


@pytest.mark.parametrize(
    ("cache_url", "expected"),
    [(CACHE_URL, ("cache.internal", 6381, 2)), (None, ("queue.internal", 6380, 0))],
    ids=["its own instance", "the queue instance when unset"],
)
async def test_cache_redis_connects_to_the_configured_url_and_falls_back_to_the_queue_url(
    environment_settings: Settings, cache_url: RedisDsn | None, expected: tuple[str, int, int]
) -> None:
    pool = build_cache_redis(settings_with_cache(environment_settings, queue=QUEUE_URL, cache=cache_url))
    try:
        kwargs = pool.connection_pool.connection_kwargs
        assert (kwargs["host"], kwargs["port"], kwargs["db"]) == expected
        assert kwargs["socket_connect_timeout"] == READ_TIMEOUT_SECONDS
        assert kwargs["socket_timeout"] == READ_TIMEOUT_SECONDS
    finally:
        await pool.aclose()


async def test_cache_redis_that_freezes_fails_a_command_within_the_read_timeout(
    environment_settings: Settings,
) -> None:
    redis = FreezingRedis(freezes_at=CACHE_READ)
    async with serving(redis) as arq_settings:
        url = RedisDsn(f"redis://{arq_settings.host}:{arq_settings.port}/0")
        pool = build_cache_redis(settings_with_cache(environment_settings, queue=QUEUE_URL, cache=url))

        async def read() -> object:
            return await pool.get(CACHE_KEY)

        try:
            failure = await failure_within_safety_net(read())
        finally:
            await pool.aclose()

    assert redis.frozen
    assert isinstance(failure, RedisTimeoutError)
