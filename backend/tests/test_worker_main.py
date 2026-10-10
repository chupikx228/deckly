import asyncio
import contextlib
import os
from dataclasses import replace
from pathlib import Path

import pytest
from arq.connections import RedisSettings as ArqRedisSettings
from pydantic import RedisDsn
from redis.exceptions import TimeoutError as RedisTimeoutError

from deckly.config import RedisSettings, Settings
from deckly.worker.main import build_worker, connect_queue
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
READ_TIMEOUT_SECONDS = 1
NO_RETRIES = 0
EMPTY_ARRAY = b"*0\r\n"


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
