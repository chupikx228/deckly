import asyncio
import contextlib
import gc
import logging
import socket
from collections.abc import AsyncIterator, Coroutine

import pytest
from arq.connections import ArqRedis, RedisSettings
from redis.exceptions import TimeoutError as RedisTimeoutError

from deckly.infrastructure.queue import ArqJobQueue, create_queue_pool, create_redis_settings
from tests.domain.builders import JOB_ID

pytestmark = pytest.mark.anyio

LOOPBACK = "127.0.0.1"
QUEUE_LOGGER = "deckly.infrastructure.queue"
COMMAND_TIMEOUT_SECONDS = 0.05
CONNECT_TIMEOUT_SECONDS = 1
SAFETY_NET_SECONDS = 5
STARTUP_RETRIES = 1
OK_REPLY = b"+OK\r\n"
PING = b"PING"
EXISTS_AFTER_WATCH = b"EXISTS"
UNRELEASED_PIPELINE_WARNING = "ignore:Unclosed client session:ResourceWarning"


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


async def never_answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read()
    writer.close()


async def read_command(reader: asyncio.StreamReader) -> list[bytes]:
    header = await reader.readline()
    if not header:
        return []
    arguments: list[bytes] = []
    for _ in range(int(header[1:])):
        length = int((await reader.readline())[1:])
        arguments.append((await reader.readexactly(length + 2))[:-2])
    return arguments


class FreezingRedis:
    def __init__(self, freezes_at: bytes) -> None:
        self.freezes_at = freezes_at
        self.frozen = False
        self.connections = 0

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
            while command := await read_command(reader):
                self.frozen = self.frozen or command[0].upper() == self.freezes_at
                if not self.frozen:
                    writer.write(OK_REPLY)
                    await writer.drain()
        writer.close()


@contextlib.asynccontextmanager
async def serving(redis: FreezingRedis) -> AsyncIterator[RedisSettings]:
    server = await asyncio.start_server(redis.serve, LOOPBACK, 0)
    try:
        yield create_redis_settings(
            f"redis://{LOOPBACK}:{server.sockets[0].getsockname()[1]}/0",
            connect_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            connect_retries=STARTUP_RETRIES,
        )
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def silent_redis() -> AsyncIterator[ArqJobQueue]:
    server = await asyncio.start_server(never_answer, LOOPBACK, 0)
    pool = ArqRedis(host=LOOPBACK, port=int(server.sockets[0].getsockname()[1]))
    try:
        yield ArqJobQueue(pool, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS)
    finally:
        await pool.aclose()
        server.close()
        await server.wait_closed()


async def failure_within_safety_net(call: Coroutine[object, object, object]) -> BaseException | None:
    task = asyncio.create_task(call)
    await asyncio.wait({task}, timeout=SAFETY_NET_SECONDS)
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        message = "the queue was still waiting on Redis when the safety net ran out"
        raise AssertionError(message)
    return task.exception()


def abort_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == "job_abort_not_signalled"]


async def test_abort_that_cannot_reach_redis_is_logged_instead_of_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = ArqRedis(host=LOOPBACK, port=unused_port())
    try:
        with caplog.at_level(logging.ERROR, logger=QUEUE_LOGGER):
            await ArqJobQueue(pool, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS).abort(JOB_ID)
    finally:
        await pool.aclose()

    [record] = abort_failures(caplog)
    assert record.__dict__["job_id"] == str(JOB_ID)
    assert record.exc_info is not None


async def test_abort_that_redis_never_answers_gives_up_after_its_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with silent_redis() as queue:
        with caplog.at_level(logging.ERROR, logger=QUEUE_LOGGER):
            failure = await failure_within_safety_net(queue.abort(JOB_ID))

    assert failure is None
    [record] = abort_failures(caplog)
    assert record.__dict__["job_id"] == str(JOB_ID)


async def test_enqueue_that_redis_never_answers_fails_after_its_timeout() -> None:
    async with silent_redis() as queue:
        failure = await failure_within_safety_net(queue.enqueue(JOB_ID))

    assert isinstance(failure, TimeoutError)


@pytest.mark.filterwarnings(UNRELEASED_PIPELINE_WARNING)
async def test_enqueue_that_redis_freezes_during_is_bounded_through_its_cleanup() -> None:
    redis = FreezingRedis(freezes_at=EXISTS_AFTER_WATCH)
    async with serving(redis) as settings:
        pool = await create_queue_pool(settings, read_timeout_seconds=COMMAND_TIMEOUT_SECONDS)
        try:
            queue = ArqJobQueue(pool, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS)
            failure = type(await failure_within_safety_net(queue.enqueue(JOB_ID)))
        finally:
            await pool.aclose()
    gc.collect()

    assert redis.frozen
    assert issubclass(failure, TimeoutError | RedisTimeoutError)


async def test_startup_check_against_a_frozen_redis_fails_once_its_retries_run_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FreezingRedis(freezes_at=PING)
    async with serving(redis) as settings:
        settings.conn_retry_delay = 0
        with caplog.at_level(logging.WARNING, logger=QUEUE_LOGGER):
            failure = await failure_within_safety_net(
                create_queue_pool(settings, read_timeout_seconds=COMMAND_TIMEOUT_SECONDS)
            )

    assert redis.frozen
    assert isinstance(failure, RedisTimeoutError)
    assert redis.connections == STARTUP_RETRIES + 1
    retries = [record for record in caplog.records if record.getMessage() == "queue_connection_retrying"]
    assert len(retries) == STARTUP_RETRIES
