import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.application.regeneration import RegenerateNote
from deckly.config import Settings
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.rate_limit import (
    KEY_PREFIX,
    RedisRegenerationLimiter,
    RegenerationWindow,
    window_key,
)
from deckly.main import API_PREFIX, create_app
from tests.domain.builders import T0
from tests.integration.conftest import open_queue_pool
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

LIMIT = 3
WINDOW_SECONDS = 60
COMMAND_TIMEOUT_SECONDS = 1.0
WINDOW = RegenerationWindow(
    limit=LIMIT, window_seconds=WINDOW_SECONDS, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS
)
WINDOW_START = T0.replace(minute=0, second=0)


@pytest.fixture
async def pool(settings: Settings) -> AsyncIterator[ArqRedis]:
    redis = await open_queue_pool(settings)
    try:
        yield redis
    finally:
        await redis.aclose()


async def purge(redis: ArqRedis, client_id: UUID) -> None:
    async for key in redis.scan_iter(match=f"{KEY_PREFIX}:{client_id}:*"):
        await redis.delete(key)


async def test_client_gets_exactly_the_limit_within_one_window(pool: ArqRedis) -> None:
    limiter = RedisRegenerationLimiter(pool, WINDOW)
    client_id = uuid4()
    try:
        for _ in range(LIMIT):
            await limiter.acquire(client_id, WINDOW_START + timedelta(seconds=10))

        with pytest.raises(RateLimitedError) as raised:
            await limiter.acquire(client_id, WINDOW_START + timedelta(seconds=10))

        assert raised.value.retry_after_seconds == WINDOW_SECONDS - 10
    finally:
        await purge(pool, client_id)


async def test_next_window_starts_a_fresh_count(pool: ArqRedis) -> None:
    limiter = RedisRegenerationLimiter(pool, WINDOW)
    client_id = uuid4()
    try:
        for _ in range(LIMIT):
            await limiter.acquire(client_id, WINDOW_START + timedelta(seconds=59))

        await limiter.acquire(client_id, WINDOW_START + timedelta(seconds=60))
    finally:
        await purge(pool, client_id)


async def test_clients_are_counted_separately(pool: ArqRedis) -> None:
    limiter = RedisRegenerationLimiter(pool, WINDOW)
    heavy, light = uuid4(), uuid4()
    try:
        for _ in range(LIMIT):
            await limiter.acquire(heavy, WINDOW_START)
        with pytest.raises(RateLimitedError):
            await limiter.acquire(heavy, WINDOW_START)

        await limiter.acquire(light, WINDOW_START)
    finally:
        await purge(pool, heavy)
        await purge(pool, light)


async def test_counter_expires_with_its_window(pool: ArqRedis) -> None:
    limiter = RedisRegenerationLimiter(pool, WINDOW)
    client_id = uuid4()
    try:
        await limiter.acquire(client_id, WINDOW_START)

        ttl = await pool.ttl(window_key(client_id, WINDOW_START, WINDOW_SECONDS))

        assert 0 < ttl <= WINDOW_SECONDS
    finally:
        await purge(pool, client_id)


async def test_concurrent_requests_never_exceed_the_limit(pool: ArqRedis) -> None:
    limiter = RedisRegenerationLimiter(pool, WINDOW)
    client_id = uuid4()
    try:
        outcomes = await asyncio.gather(
            *(limiter.acquire(client_id, WINDOW_START) for _ in range(LIMIT * 4)), return_exceptions=True
        )

        assert sum(1 for outcome in outcomes if outcome is None) == LIMIT
        assert all(isinstance(outcome, RateLimitedError) for outcome in outcomes if outcome is not None)
    finally:
        await purge(pool, client_id)


async def test_unreachable_redis_is_an_upstream_outage_not_a_free_pass() -> None:
    unreachable = ArqRedis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    limiter = RedisRegenerationLimiter(unreachable, WINDOW)
    try:
        with pytest.raises(UpstreamUnavailableError):
            await limiter.acquire(uuid4(), WINDOW_START)
    finally:
        await unreachable.aclose()


async def fill_window(settings: Settings, client_id: UUID) -> None:
    redis = await open_queue_pool(settings)
    window_seconds = settings.limits.note_regeneration_window_seconds
    now = utc_now()
    try:
        for moment in (now, now + timedelta(seconds=window_seconds)):
            key = window_key(client_id, moment, window_seconds)
            await redis.set(key, settings.limits.note_regenerations_per_window, ex=window_seconds * 2)
    finally:
        await redis.aclose()


async def purge_client(settings: Settings, client_id: UUID) -> None:
    redis = await open_queue_pool(settings)
    try:
        await purge(redis, client_id)
    finally:
        await redis.aclose()


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield create_app(settings)
    finally:
        root.handlers, root.level = handlers, level


def test_app_wires_regeneration_and_refuses_an_exhausted_client_before_any_provider(
    settings: Settings, app: FastAPI
) -> None:
    client_id = uuid4()
    asyncio.run(fill_window(settings, client_id))
    try:
        with TestClient(app) as app_client:
            assert isinstance(app.state.regenerate_note, RegenerateNote)
            response = app_client.post(
                f"{API_PREFIX}/notes/regenerate",
                json={
                    "topic": "Road signs",
                    "language": "ru",
                    "noteType": "basic",
                    "rejectedNote": {"fields": {"front": "a", "back": "b"}},
                    "reason": "duplicate",
                },
                headers={"X-Client-Id": str(client_id)},
            )

        assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS, response.text
        body = response.json()
        assert spec_errors("Problem", body) == []
        assert body["code"] == "RATE_LIMITED"
        assert 0 < body["retryAfterSeconds"] <= settings.limits.note_regeneration_window_seconds
    finally:
        asyncio.run(purge_client(settings, client_id))
