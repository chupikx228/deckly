import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx2
import pytest
from arq.connections import ArqRedis

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.application.ports import Quota, Requester
from deckly.config import Settings
from deckly.infrastructure.quota import (
    QUOTA_WINDOW,
    QuotaLimits,
    RedisGenerationQuota,
    address_key,
    client_key,
)
from deckly.main import API_PREFIX, create_app
from tests.fakes import FakeTopicModerator
from tests.integration.conftest import Cleanup, purge_quota, screen_topics_with, with_generation_limits
from tests.integration.test_health_endpoint import LOOPBACK, Proxy
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PER_CLIENT = 3
PER_ADDRESS = 5
COMMAND_TIMEOUT_SECONDS = 0.5
LIMITS = QuotaLimits(
    jobs_per_client=PER_CLIENT, jobs_per_address=PER_ADDRESS, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS
)
DAY_START = datetime(2026, 8, 14, tzinfo=UTC)
NOON = DAY_START + timedelta(hours=12)
NEXT_DAY = DAY_START + QUOTA_WINDOW
WINDOW_SECONDS = int(QUOTA_WINDOW.total_seconds())
CONCURRENT_REQUESTS = 12
SAFETY_NET_SECONDS = 10
GENERATIONS = f"{API_PREFIX}/generations"
HEALTH = f"{API_PREFIX}/health"
PEER_PORT = 50000
MINIMAL = {"topic": "Road signs", "language": "ru", "cardCount": 40}


def unique_ipv4() -> str:
    host = uuid4().int
    return f"10.{host % 256}.{host // 256 % 256}.{host // 65536 % 256}"


def unique_ipv6_network() -> str:
    host = uuid4().int
    return f"2001:db8:{host % 65536:x}:{host // 65536 % 65536:x}"


class Requesters:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool
        self._client_ids: set[UUID] = set()
        self._addresses: set[str] = set()

    def new(self, address: str | None = None) -> Requester:
        requester = Requester(client_id=uuid4(), address=unique_ipv4() if address is None else address)
        self.track(requester)
        return requester

    def track(self, requester: Requester) -> None:
        self._client_ids.add(requester.client_id)
        self._addresses.add(requester.address)

    async def purge(self) -> None:
        await purge_quota(self._pool, self._client_ids, self._addresses)


@pytest.fixture
async def requesters(queue_pool: ArqRedis) -> AsyncIterator[Requesters]:
    tracker = Requesters(queue_pool)
    try:
        yield tracker
    finally:
        await tracker.purge()


@pytest.fixture
def quota(queue_pool: ArqRedis) -> RedisGenerationQuota:
    return RedisGenerationQuota(queue_pool, LIMITS)


async def test_client_gets_exactly_its_limit_then_a_429_until_the_next_utc_midnight(
    quota: RedisGenerationQuota, requesters: Requesters
) -> None:
    requester = requesters.new()

    reserved = [await quota.reserve(requester, NOON) for _ in range(PER_CLIENT)]

    assert [each.remaining for each in reserved] == [2, 1, 0]
    assert {each.resets_at for each in reserved} == {NEXT_DAY}
    with pytest.raises(RateLimitedError) as raised:
        await quota.reserve(requester, NOON)
    assert raised.value.retry_after_seconds == WINDOW_SECONDS // 2
    assert await quota.current(requester.client_id, NOON) == Quota(
        limit=PER_CLIENT, remaining=0, resets_at=NEXT_DAY
    )


async def test_refused_attempts_use_no_budget(quota: RedisGenerationQuota, requesters: Requesters) -> None:
    requester = requesters.new()
    for _ in range(PER_CLIENT):
        await quota.reserve(requester, NOON)

    for _ in range(PER_ADDRESS):
        with pytest.raises(RateLimitedError):
            await quota.reserve(requester, NOON)

    neighbour = requesters.new(requester.address)
    for _ in range(PER_ADDRESS - PER_CLIENT):
        await quota.reserve(neighbour, NOON)
    assert await quota.current(requester.client_id, NOON) == Quota(
        limit=PER_CLIENT, remaining=0, resets_at=NEXT_DAY
    )


async def test_quota_read_before_any_job_is_the_full_budget_until_the_next_midnight(
    quota: RedisGenerationQuota, requesters: Requesters
) -> None:
    requester = requesters.new()

    assert await quota.current(requester.client_id, DAY_START) == Quota(
        limit=PER_CLIENT, remaining=PER_CLIENT, resets_at=NEXT_DAY
    )
    assert await quota.current(requester.client_id, NOON) == Quota(
        limit=PER_CLIENT, remaining=PER_CLIENT, resets_at=NEXT_DAY
    )


async def test_next_utc_day_starts_a_fresh_budget(
    quota: RedisGenerationQuota, requesters: Requesters
) -> None:
    requester = requesters.new()
    last_second = NEXT_DAY - timedelta(seconds=1)
    for _ in range(PER_CLIENT):
        await quota.reserve(requester, last_second)
    with pytest.raises(RateLimitedError) as raised:
        await quota.reserve(requester, last_second)
    assert raised.value.retry_after_seconds == 1

    fresh = await quota.reserve(requester, NEXT_DAY)

    assert fresh == Quota(limit=PER_CLIENT, remaining=PER_CLIENT - 1, resets_at=NEXT_DAY + QUOTA_WINDOW)


async def test_rotating_client_ids_from_one_address_is_stopped_by_the_backstop(
    quota: RedisGenerationQuota, requesters: Requesters
) -> None:
    address = unique_ipv4()
    spoofed = [requesters.new(address) for _ in range(PER_ADDRESS)]
    for requester in spoofed:
        await quota.reserve(requester, NOON)

    with pytest.raises(RateLimitedError) as raised:
        await quota.reserve(requesters.new(address), NOON)

    assert raised.value.retry_after_seconds == WINDOW_SECONDS // 2
    assert (await quota.reserve(requesters.new(), NOON)).remaining == PER_CLIENT - 1
    assert [(await quota.current(each.client_id, NOON)).remaining for each in spoofed] == [
        PER_CLIENT - 1
    ] * PER_ADDRESS


async def test_rotating_addresses_within_one_ipv6_64_is_stopped_by_the_backstop(
    quota: RedisGenerationQuota, requesters: Requesters
) -> None:
    network = unique_ipv6_network()
    for host in range(1, PER_ADDRESS + 1):
        await quota.reserve(requesters.new(f"{network}::{host:x}"), NOON)

    with pytest.raises(RateLimitedError):
        await quota.reserve(requesters.new(f"{network}:ffff:ffff:ffff:ffff"), NOON)


async def test_concurrent_reservations_never_exceed_the_client_limit(
    quota: RedisGenerationQuota, requesters: Requesters
) -> None:
    requester = requesters.new()

    outcomes = await asyncio.gather(
        *(quota.reserve(requester, NOON) for _ in range(CONCURRENT_REQUESTS)), return_exceptions=True
    )

    admitted = [outcome for outcome in outcomes if isinstance(outcome, Quota)]
    assert sorted(each.remaining for each in admitted) == [0, 1, 2]
    assert all(isinstance(outcome, RateLimitedError) for outcome in outcomes if outcome not in admitted)


async def test_concurrent_reservations_never_exceed_the_address_limit(
    quota: RedisGenerationQuota, requesters: Requesters
) -> None:
    address = unique_ipv4()
    spoofed = [requesters.new(address) for _ in range(CONCURRENT_REQUESTS)]

    outcomes = await asyncio.gather(
        *(quota.reserve(requester, NOON) for requester in spoofed), return_exceptions=True
    )

    assert sum(1 for outcome in outcomes if isinstance(outcome, Quota)) == PER_ADDRESS
    assert sum(1 for outcome in outcomes if isinstance(outcome, RateLimitedError)) == (
        CONCURRENT_REQUESTS - PER_ADDRESS
    )


async def test_release_gives_back_both_the_client_and_the_address_unit(
    quota: RedisGenerationQuota, requesters: Requesters, queue_pool: ArqRedis
) -> None:
    requester = requesters.new()
    await quota.reserve(requester, NOON)

    await quota.release(requester, NOON)

    assert (await quota.current(requester.client_id, NOON)).remaining == PER_CLIENT
    assert await queue_pool.get(address_key(requester.address, NOON)) == b"0"


async def test_release_never_takes_a_counter_below_zero(
    quota: RedisGenerationQuota, requesters: Requesters, queue_pool: ArqRedis
) -> None:
    requester = requesters.new()
    await quota.reserve(requester, NOON)

    await quota.release(requester, NOON)
    await quota.release(requester, NOON)

    assert await queue_pool.get(client_key(requester.client_id, NOON)) == b"0"
    assert (await quota.current(requester.client_id, NOON)).remaining == PER_CLIENT


async def test_release_without_a_reservation_leaves_no_key_behind(
    quota: RedisGenerationQuota, requesters: Requesters, queue_pool: ArqRedis
) -> None:
    requester = requesters.new()

    await quota.release(requester, NOON)

    assert (
        await queue_pool.exists(client_key(requester.client_id, NOON), address_key(requester.address, NOON))
        == 0
    )


async def test_counters_expire_with_their_window(
    quota: RedisGenerationQuota, requesters: Requesters, queue_pool: ArqRedis
) -> None:
    requester = requesters.new()

    await quota.reserve(requester, NOON)

    for key in (client_key(requester.client_id, NOON), address_key(requester.address, NOON)):
        assert 0 < await queue_pool.ttl(key) <= WINDOW_SECONDS


async def test_unreachable_redis_refuses_rather_than_running_unmetered() -> None:
    unreachable = ArqRedis(host=LOOPBACK, port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    quota = RedisGenerationQuota(unreachable, LIMITS)
    requester = Requester(client_id=uuid4(), address=unique_ipv4())
    try:
        with pytest.raises(UpstreamUnavailableError):
            await quota.reserve(requester, NOON)
        with pytest.raises(UpstreamUnavailableError):
            await quota.current(requester.client_id, NOON)
        await quota.release(requester, NOON)
    finally:
        await unreachable.aclose()


async def test_frozen_redis_refuses_within_the_command_timeout(
    settings: Settings, requesters: Requesters
) -> None:
    proxy = Proxy(settings.redis.url.host or LOOPBACK, settings.redis.url.port or 0)
    frozen = ArqRedis(host=LOOPBACK, port=await proxy.start(), socket_timeout=SAFETY_NET_SECONDS)
    quota = RedisGenerationQuota(frozen, LIMITS)
    requester = requesters.new()
    loop = asyncio.get_running_loop()
    try:
        await frozen.ping()
        proxy.freeze()
        calls: list[Callable[[], Awaitable[Quota]]] = [
            lambda: quota.reserve(requester, NOON),
            lambda: quota.current(requester.client_id, NOON),
        ]
        for call in calls:
            started = loop.time()
            with pytest.raises(UpstreamUnavailableError):
                await call()
            assert loop.time() - started < COMMAND_TIMEOUT_SECONDS + 1
        started = loop.time()
        await quota.release(requester, NOON)
        assert loop.time() - started < COMMAND_TIMEOUT_SECONDS + 1
    finally:
        await frozen.aclose()
        await proxy.cut()
        proxy.thaw()


def headers(client_id: UUID, key: UUID | None = None) -> dict[str, str]:
    return {"X-Client-Id": str(client_id), "Idempotency-Key": str(uuid4() if key is None else key)}


async def post(client: httpx2.AsyncClient, cleanup: Cleanup, sent: dict[str, str]) -> httpx2.Response:
    cleanup.client_ids.add(UUID(sent["X-Client-Id"]))
    async with asyncio.timeout(SAFETY_NET_SECONDS):
        response = await client.post(GENERATIONS, json=MINIMAL, headers=sent)
    if response.status_code == HTTPStatus.ACCEPTED:
        cleanup.job_ids.add(UUID(response.json()["jobId"]))
    return response


def assert_rate_limited(response: httpx2.Response) -> None:
    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS, response.text
    body = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == "RATE_LIMITED"
    assert 0 < body["retryAfterSeconds"] <= WINDOW_SECONDS
    assert response.headers["Retry-After"] == str(body["retryAfterSeconds"])


@pytest.fixture
def topic_moderator() -> FakeTopicModerator:
    return FakeTopicModerator()


@pytest.fixture
async def app_client(
    settings: Settings, restored_logging: None, requesters: Requesters, topic_moderator: FakeTopicModerator
) -> AsyncIterator[tuple[httpx2.AsyncClient, str]]:
    del restored_logging
    address = unique_ipv4()
    requesters.track(Requester(client_id=uuid4(), address=address))
    app = create_app(with_generation_limits(settings, per_client=2, per_address=3))
    transport = httpx2.ASGITransport(app, client=(address, PEER_PORT))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://deckly") as client,
    ):
        screen_topics_with(app, topic_moderator)
        yield client, address


async def test_app_rejects_a_policy_violating_topic_with_a_422_stores_nothing_and_spends_a_unit(
    app_client: tuple[httpx2.AsyncClient, str],
    cleanup: Cleanup,
    requesters: Requesters,
    topic_moderator: FakeTopicModerator,
) -> None:
    client, address = app_client
    client_id, key = uuid4(), uuid4()
    requesters.track(Requester(client_id=client_id, address=address))
    topic_moderator.outcome = False

    rejected = await post(client, cleanup, headers(client_id, key))
    health = await client.get(HEALTH, headers={"X-Client-Id": str(client_id)})
    topic_moderator.outcome = True
    accepted = await post(client, cleanup, headers(client_id, key))

    assert rejected.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, rejected.text
    assert spec_errors("Problem", rejected.json()) == []
    assert rejected.json()["code"] == "TOPIC_REJECTED"
    assert health.json()["quota"]["remaining"] == 1
    assert accepted.status_code == HTTPStatus.ACCEPTED, accepted.text
    assert accepted.json()["quota"]["remaining"] == 0


async def test_app_gives_the_unit_back_when_the_topic_cannot_be_checked(
    app_client: tuple[httpx2.AsyncClient, str],
    cleanup: Cleanup,
    requesters: Requesters,
    topic_moderator: FakeTopicModerator,
) -> None:
    client, address = app_client
    client_id = uuid4()
    requesters.track(Requester(client_id=client_id, address=address))
    topic_moderator.outcome = UpstreamUnavailableError(5)

    refused = await post(client, cleanup, headers(client_id))

    assert refused.status_code == HTTPStatus.SERVICE_UNAVAILABLE, refused.text
    assert refused.json()["code"] == "UPSTREAM_UNAVAILABLE"
    health = await client.get(HEALTH, headers={"X-Client-Id": str(client_id)})
    assert health.json()["quota"]["remaining"] == 2


async def test_app_charges_a_new_job_once_and_reports_the_same_quota_on_health(
    app_client: tuple[httpx2.AsyncClient, str], cleanup: Cleanup, requesters: Requesters
) -> None:
    client, address = app_client
    client_id, key = uuid4(), uuid4()
    requesters.track(Requester(client_id=client_id, address=address))

    first = await post(client, cleanup, headers(client_id, key))
    replay = await post(client, cleanup, headers(client_id, key))
    health = await client.get(HEALTH, headers={"X-Client-Id": str(client_id)})

    assert first.status_code == replay.status_code == HTTPStatus.ACCEPTED
    assert first.json()["jobId"] == replay.json()["jobId"]
    assert first.json()["quota"]["remaining"] == replay.json()["quota"]["remaining"] == 1
    assert health.json()["quota"] == first.json()["quota"]
    assert first.json()["quota"]["resetsAt"].endswith("T00:00:00Z")


async def test_app_refuses_an_exhausted_client_with_a_429_and_stores_nothing(
    app_client: tuple[httpx2.AsyncClient, str], cleanup: Cleanup, requesters: Requesters
) -> None:
    client, address = app_client
    client_id = uuid4()
    requesters.track(Requester(client_id=client_id, address=address))
    for _ in range(2):
        assert (await post(client, cleanup, headers(client_id))).status_code == HTTPStatus.ACCEPTED

    refused = await post(client, cleanup, headers(client_id))

    assert_rate_limited(refused)
    assert len(cleanup.job_ids) == 2
    health = await client.get(HEALTH, headers={"X-Client-Id": str(client_id)})
    assert health.json()["quota"]["remaining"] == 0


async def test_app_stops_spoofed_client_ids_from_one_address(
    app_client: tuple[httpx2.AsyncClient, str], cleanup: Cleanup, requesters: Requesters
) -> None:
    client, address = app_client
    spoofed = [uuid4() for _ in range(4)]
    for client_id in spoofed:
        requesters.track(Requester(client_id=client_id, address=address))

    responses = [await post(client, cleanup, headers(client_id)) for client_id in spoofed]

    assert [response.status_code for response in responses[:3]] == [HTTPStatus.ACCEPTED] * 3
    assert_rate_limited(responses[3])


async def test_app_ignores_a_forwarded_header_when_counting_the_address(
    app_client: tuple[httpx2.AsyncClient, str], cleanup: Cleanup, requesters: Requesters
) -> None:
    client, address = app_client
    spoofed = [uuid4() for _ in range(4)]
    for client_id in spoofed:
        requesters.track(Requester(client_id=client_id, address=address))

    responses = [
        await post(client, cleanup, {**headers(client_id), "X-Forwarded-For": unique_ipv4()})
        for client_id in spoofed
    ]

    assert_rate_limited(responses[3])
