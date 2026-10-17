from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from arq.connections import ArqRedis

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.application.ports import Requester
from deckly.config import Settings
from deckly.infrastructure.observability.metrics import Metrics, RateLimit
from deckly.infrastructure.rate_limit import window_index
from deckly.infrastructure.upload_limit import (
    RECORDING_URL_ADDRESS_KEY_PREFIX,
    RECORDING_URL_CLIENT_KEY_PREFIX,
    RECORDING_URL_SERVER_KEY_PREFIX,
    FetchWindow,
    RedisRecordingUrlLimiter,
)
from tests.domain.builders import T0
from tests.fakes import fresh_metrics
from tests.integration.conftest import open_queue_pool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

WINDOW = FetchWindow(
    fetches_per_client=2,
    fetches_per_address=3,
    fetches_per_server=4,
    window_seconds=60,
    command_timeout_seconds=1.0,
)
WINDOW_START = T0.replace(year=1999, minute=0, second=0)
TEST_WINDOWS = (
    window_index(WINDOW_START, WINDOW.window_seconds),
    window_index(WINDOW_START, WINDOW.window_seconds) + 1,
)


@pytest.fixture
async def pool(settings: Settings) -> AsyncIterator[ArqRedis]:
    redis = await open_queue_pool(settings)
    try:
        yield redis
    finally:
        for prefix in (
            RECORDING_URL_CLIENT_KEY_PREFIX,
            RECORDING_URL_ADDRESS_KEY_PREFIX,
            RECORDING_URL_SERVER_KEY_PREFIX,
        ):
            for index in TEST_WINDOWS:
                async for key in redis.scan_iter(match=f"{prefix}:*:{index}"):
                    await redis.delete(key)
        await redis.aclose()


def requester(address: str) -> Requester:
    return Requester(client_id=uuid4(), address=address)


def rejected_limit(metrics: Metrics, limit: RateLimit) -> float | None:
    return metrics.registry.get_sample_value("deckly_rate_limit_rejections_total", {"limit": limit})


async def test_client_gets_exactly_its_fetches_and_then_429_until_the_window_ends(pool: ArqRedis) -> None:
    metrics = fresh_metrics()
    limiter = RedisRecordingUrlLimiter(pool, WINDOW, metrics)
    client = requester("198.51.100.10")

    for _ in range(WINDOW.fetches_per_client):
        await limiter.acquire(client, WINDOW_START)
    with pytest.raises(RateLimitedError) as raised:
        await limiter.acquire(client, WINDOW_START + timedelta(seconds=15))

    assert raised.value.retry_after_seconds == 45
    assert rejected_limit(metrics, RateLimit.RECORDING_URL_CLIENT) == 1
    await limiter.acquire(client, WINDOW_START + timedelta(seconds=WINDOW.window_seconds))


async def test_rotating_client_ids_from_one_address_hits_the_address_limit(pool: ArqRedis) -> None:
    metrics = fresh_metrics()
    limiter = RedisRecordingUrlLimiter(pool, WINDOW, metrics)

    for _ in range(WINDOW.fetches_per_address):
        await limiter.acquire(requester("198.51.100.20"), WINDOW_START)
    with pytest.raises(RateLimitedError):
        await limiter.acquire(requester("198.51.100.20"), WINDOW_START)

    assert rejected_limit(metrics, RateLimit.RECORDING_URL_ADDRESS) == 1


async def test_many_addresses_together_hit_the_server_wide_limit(pool: ArqRedis) -> None:
    metrics = fresh_metrics()
    limiter = RedisRecordingUrlLimiter(pool, WINDOW, metrics)

    for number in range(WINDOW.fetches_per_server):
        await limiter.acquire(requester(f"203.0.113.{number + 1}"), WINDOW_START)
    with pytest.raises(RateLimitedError):
        await limiter.acquire(requester("192.0.2.200"), WINDOW_START)

    assert rejected_limit(metrics, RateLimit.RECORDING_URL_SERVER) == 1


async def test_recording_url_keys_expire_with_their_window(pool: ArqRedis) -> None:
    limiter = RedisRecordingUrlLimiter(pool, WINDOW, fresh_metrics())

    await limiter.acquire(requester("198.51.100.30"), WINDOW_START)

    keys = [key async for key in pool.scan_iter(match=f"deckly:recording-url:*:{TEST_WINDOWS[0]}")]
    assert len(keys) == 3
    for key in keys:
        assert 0 < await pool.ttl(key) <= WINDOW.window_seconds


async def test_fetch_budget_is_separate_from_the_upload_budget(pool: ArqRedis) -> None:
    limiter = RedisRecordingUrlLimiter(pool, WINDOW, fresh_metrics())

    await limiter.acquire(requester("198.51.100.40"), WINDOW_START)

    assert [key async for key in pool.scan_iter(match=f"deckly:document-upload:*:{TEST_WINDOWS[0]}")] == []


async def test_unreachable_redis_is_503_not_an_unmetered_fetch() -> None:
    unreachable = ArqRedis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    limiter = RedisRecordingUrlLimiter(
        unreachable, replace(WINDOW, command_timeout_seconds=0.5), fresh_metrics()
    )

    with pytest.raises(UpstreamUnavailableError):
        await limiter.acquire(requester("198.51.100.50"), WINDOW_START)

    await unreachable.aclose()
