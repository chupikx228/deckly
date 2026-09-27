import asyncio
import contextlib
import logging
import struct
from collections.abc import AsyncIterator, Awaitable

import pytest

from deckly.infrastructure.database import create_engine
from deckly.infrastructure.health import BoundedProbe, Dependency, postgres_probe, redis_probe
from deckly.infrastructure.queue import create_queue_pool, create_redis_settings
from tests.infrastructure.test_queue import LOOPBACK, FreezingRedis, never_answer, serving, unused_port

pytestmark = pytest.mark.anyio

HEALTH_LOGGER = "deckly.infrastructure.health"
PROBE_TIMEOUT_SECONDS = 0.1
LONG_READ_TIMEOUT_SECONDS = 30
SHORT_READ_TIMEOUT_SECONDS = 0.05
SAFETY_NET_SECONDS = 5
NAME = "dependency"
UNUSED_COMMAND = b"UNUSED"


class CheckFailedError(Exception):
    pass


class Check:
    def __init__(self) -> None:
        self.calls = 0
        self.cancellations = 0
        self.release = asyncio.Event()
        self.cleanup_released = asyncio.Event()
        self.cleanup_released.set()
        self.failure: Exception | None = None
        self.task: asyncio.Task[object] | None = None

    async def __call__(self) -> None:
        self.calls += 1
        self.task = asyncio.current_task()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            await self.cleanup_released.wait()
            raise
        if self.failure is not None:
            raise self.failure


def probe_for(check: Check) -> BoundedProbe:
    return BoundedProbe(
        Dependency(name=NAME, check=check, failures=(CheckFailedError,)),
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )


def answered() -> Check:
    check = Check()
    check.release.set()
    return check


async def within_safety_net(call: Awaitable[bool]) -> tuple[bool, float]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(SAFETY_NET_SECONDS):
        healthy = await call
    return healthy, loop.time() - started


def messages(caplog: pytest.LogCaptureFixture) -> list[tuple[str, object]]:
    return [(record.getMessage(), record.__dict__.get("dependency")) for record in caplog.records]


async def test_answering_dependency_is_healthy() -> None:
    assert await probe_for(answered()).is_healthy()


async def test_expected_failure_is_unhealthy_and_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    check = answered()
    check.failure = CheckFailedError()

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        assert not await probe_for(check).is_healthy()

    assert messages(caplog) == [("dependency_unreachable", NAME)]
    [record] = caplog.records
    assert (record.levelno, record.exc_info) == (logging.WARNING, None)


async def test_unforeseen_failure_is_unhealthy_and_logged_as_an_error_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    check = answered()
    check.failure = ValueError()

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        assert not await probe_for(check).is_healthy()

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is check.failure


async def test_unanswered_check_is_unhealthy_once_its_timeout_runs_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    check = Check()

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        healthy, elapsed = await within_safety_net(probe_for(check).is_healthy())
    await asyncio.sleep(0)

    assert not healthy
    assert PROBE_TIMEOUT_SECONDS <= elapsed < SAFETY_NET_SECONDS
    assert check.cancellations == 1
    assert messages(caplog) == [("dependency_unresponsive", NAME)]


async def test_concurrent_callers_share_one_check() -> None:
    check = Check()
    probe = probe_for(check)
    callers = [asyncio.create_task(probe.is_healthy()) for _ in range(5)]
    await asyncio.sleep(0)
    check.release.set()

    assert await asyncio.gather(*callers) == [True] * 5
    assert check.calls == 1


async def test_caller_that_joined_a_check_another_caller_gave_up_on_is_unhealthy() -> None:
    check = Check()
    probe = probe_for(check)
    first = asyncio.create_task(probe.is_healthy())
    await asyncio.sleep(PROBE_TIMEOUT_SECONDS / 2)
    joined = asyncio.create_task(probe.is_healthy())

    assert (await within_safety_net(first))[0] is False
    assert (await within_safety_net(joined))[0] is False
    assert check.calls == 1


async def test_next_call_after_an_answer_checks_again() -> None:
    check = answered()
    probe = probe_for(check)

    assert await probe.is_healthy()
    assert await probe.is_healthy()
    assert check.calls == 2


async def test_check_stuck_in_its_cleanup_is_cancelled_once_and_reported_without_waiting() -> None:
    check = Check()
    check.cleanup_released.clear()
    probe = probe_for(check)

    first, _ = await within_safety_net(probe.is_healthy())
    second, elapsed = await within_safety_net(probe.is_healthy())

    await asyncio.sleep(0)

    assert (first, second) == (False, False)
    assert elapsed < PROBE_TIMEOUT_SECONDS
    assert check.task is not None
    assert (check.calls, check.cancellations, check.task.cancelling()) == (1, 1, 1)

    check.cleanup_released.set()
    check.release.set()
    await asyncio.sleep(0)

    assert await probe.is_healthy()
    assert check.calls == 2


@contextlib.asynccontextmanager
async def silent_server() -> AsyncIterator[int]:
    server = await asyncio.start_server(never_answer, LOOPBACK, 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


def postgres_url(port: int) -> str:
    return f"postgresql+asyncpg://deckly:deckly@{LOOPBACK}:{port}/deckly"


async def postgres_healthy(port: int) -> tuple[bool, float]:
    engine = create_engine(postgres_url(port), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        return await within_safety_net(
            postgres_probe(engine, timeout_seconds=PROBE_TIMEOUT_SECONDS).is_healthy()
        )
    finally:
        await engine.dispose()


async def test_postgres_on_a_closed_port_is_unhealthy_without_waiting_out_the_timeout() -> None:
    healthy, elapsed = await postgres_healthy(unused_port())

    assert not healthy
    assert elapsed < PROBE_TIMEOUT_SECONDS


async def test_postgres_that_never_answers_is_unhealthy_once_the_timeout_runs_out() -> None:
    async with silent_server() as port:
        healthy, elapsed = await postgres_healthy(port)

    assert not healthy
    assert PROBE_TIMEOUT_SECONDS <= elapsed < SAFETY_NET_SECONDS


SSL_REQUEST_BYTES = 8
SSL_REFUSED = b"N"
STARTING_UP_FIELDS = b"SFATAL\x00VFATAL\x00C57P03\x00Mthe database system is starting up\x00\x00"
STARTING_UP = b"E" + struct.pack("!i", len(STARTING_UP_FIELDS) + 4) + STARTING_UP_FIELDS


async def starting_up(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
        await reader.readexactly(SSL_REQUEST_BYTES)
        writer.write(SSL_REFUSED)
        length = struct.unpack("!i", await reader.readexactly(4))[0]
        await reader.readexactly(length - 4)
        writer.write(STARTING_UP)
        await writer.drain()
    writer.close()


async def test_postgres_that_is_still_starting_up_is_unhealthy() -> None:
    server = await asyncio.start_server(starting_up, LOOPBACK, 0)
    try:
        healthy, _ = await postgres_healthy(int(server.sockets[0].getsockname()[1]))
    finally:
        server.close()
        await server.wait_closed()

    assert not healthy


@pytest.mark.parametrize("read_timeout_seconds", [SHORT_READ_TIMEOUT_SECONDS, LONG_READ_TIMEOUT_SECONDS])
async def test_redis_that_freezes_after_startup_is_unhealthy_within_the_bound(
    read_timeout_seconds: float,
) -> None:
    redis = FreezingRedis(freezes_at=UNUSED_COMMAND)
    async with serving(redis) as settings:
        pool = await create_queue_pool(settings, read_timeout_seconds=read_timeout_seconds)
        try:
            probe = redis_probe(pool, timeout_seconds=PROBE_TIMEOUT_SECONDS)
            assert await probe.is_healthy()
            redis.frozen = True
            healthy, elapsed = await within_safety_net(probe.is_healthy())
        finally:
            await pool.aclose()

    assert not healthy
    assert elapsed < SAFETY_NET_SECONDS


class HangingUpRedis(FreezingRedis):
    def __init__(self) -> None:
        super().__init__(freezes_at=UNUSED_COMMAND)
        self.writers: list[asyncio.StreamWriter] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        await super().serve(reader, writer)


async def test_redis_that_went_away_after_startup_is_unhealthy() -> None:
    redis = HangingUpRedis()
    server = await asyncio.start_server(redis.serve, LOOPBACK, 0)
    url = f"redis://{LOOPBACK}:{server.sockets[0].getsockname()[1]}/0"
    pool = await create_queue_pool(
        create_redis_settings(url, connect_timeout_seconds=1, connect_retries=0),
        read_timeout_seconds=LONG_READ_TIMEOUT_SECONDS,
    )
    try:
        server.close()
        for writer in redis.writers:
            writer.close()
        await server.wait_closed()
        healthy, elapsed = await within_safety_net(
            redis_probe(pool, timeout_seconds=PROBE_TIMEOUT_SECONDS).is_healthy()
        )
    finally:
        await pool.aclose()

    assert not healthy
    assert elapsed < PROBE_TIMEOUT_SECONDS
