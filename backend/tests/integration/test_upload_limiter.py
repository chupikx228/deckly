import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.application.ports import Requester
from deckly.config import Settings
from deckly.infrastructure.observability.metrics import RateLimit
from deckly.infrastructure.quota import address_bucket
from deckly.infrastructure.rate_limit import window_key
from deckly.infrastructure.upload_limit import (
    ADDRESS_KEY_PREFIX,
    CLIENT_KEY_PREFIX,
    RedisUploadLimiter,
    UploadWindow,
)
from tests.domain.builders import T0
from tests.fakes import fresh_metrics
from tests.integration.conftest import open_queue_pool

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PER_CLIENT = 3
PER_ADDRESS = 5
WINDOW_SECONDS = 60
WINDOW = UploadWindow(
    uploads_per_client=PER_CLIENT,
    uploads_per_address=PER_ADDRESS,
    window_seconds=WINDOW_SECONDS,
    command_timeout_seconds=1.0,
)
WINDOW_START = T0.replace(minute=0, second=0)
IPV6_HOST = "2001:db8:abcd:12::1"
IPV6_NEIGHBOUR = "2001:db8:abcd:ff::2"


class Tracked:
    def __init__(self, pool: ArqRedis) -> None:
        self.pool = pool
        self.client_ids: set[UUID] = set()
        self.addresses: set[str] = set()

    def requester(self, address: str | None = None, client_id: UUID | None = None) -> Requester:
        chosen = client_id or uuid4()
        where = address or f"198.51.100.{len(self.addresses) + 1}"
        self.client_ids.add(chosen)
        self.addresses.add(where)
        return Requester(client_id=chosen, address=where)

    async def purge(self) -> None:
        patterns = [f"{CLIENT_KEY_PREFIX}:{client_id}:*" for client_id in self.client_ids] + [
            f"{ADDRESS_KEY_PREFIX}:{address_bucket(address)}:*" for address in self.addresses
        ]
        for pattern in patterns:
            async for key in self.pool.scan_iter(match=pattern):
                await self.pool.delete(key)


@pytest.fixture
async def tracked(settings: Settings) -> AsyncIterator[Tracked]:
    pool = await open_queue_pool(settings)
    tracker = Tracked(pool)
    try:
        yield tracker
    finally:
        await tracker.purge()
        await pool.aclose()


def limiter(tracked: Tracked) -> RedisUploadLimiter:
    return RedisUploadLimiter(tracked.pool, WINDOW, fresh_metrics())


async def test_client_gets_exactly_its_limit_within_one_window(tracked: Tracked) -> None:
    uploads = limiter(tracked)
    requester = tracked.requester()
    for _ in range(PER_CLIENT):
        await uploads.acquire(requester, WINDOW_START + timedelta(seconds=10))

    with pytest.raises(RateLimitedError) as raised:
        await uploads.acquire(requester, WINDOW_START + timedelta(seconds=10))

    assert raised.value.retry_after_seconds == WINDOW_SECONDS - 10


async def test_next_window_starts_a_fresh_count(tracked: Tracked) -> None:
    uploads = limiter(tracked)
    requester = tracked.requester()
    for _ in range(PER_CLIENT):
        await uploads.acquire(requester, WINDOW_START + timedelta(seconds=59))

    await uploads.acquire(requester, WINDOW_START + timedelta(seconds=60))


async def test_rotating_client_ids_behind_one_address_trips_the_address_backstop(tracked: Tracked) -> None:
    metrics = fresh_metrics()
    uploads = RedisUploadLimiter(tracked.pool, WINDOW, metrics)
    address = "203.0.113.200"
    for _ in range(PER_ADDRESS):
        await uploads.acquire(tracked.requester(address), WINDOW_START)

    with pytest.raises(RateLimitedError):
        await uploads.acquire(tracked.requester(address), WINDOW_START)

    assert (
        metrics.registry.get_sample_value(
            "deckly_rate_limit_rejections_total", {"limit": RateLimit.DOCUMENT_UPLOAD_ADDRESS}
        )
        == 1
    )
    assert (
        metrics.registry.get_sample_value(
            "deckly_rate_limit_rejections_total", {"limit": RateLimit.DOCUMENT_UPLOAD_CLIENT}
        )
        is None
    )


async def test_ipv6_neighbours_in_one_56_share_the_address_backstop(tracked: Tracked) -> None:
    uploads = limiter(tracked)
    for number in range(PER_ADDRESS):
        await uploads.acquire(tracked.requester(IPV6_HOST if number % 2 else IPV6_NEIGHBOUR), WINDOW_START)

    with pytest.raises(RateLimitedError):
        await uploads.acquire(tracked.requester(IPV6_HOST), WINDOW_START)


async def test_clients_and_addresses_are_counted_separately(tracked: Tracked) -> None:
    uploads = limiter(tracked)
    heavy = tracked.requester()
    for _ in range(PER_CLIENT):
        await uploads.acquire(heavy, WINDOW_START)
    with pytest.raises(RateLimitedError):
        await uploads.acquire(heavy, WINDOW_START)

    await uploads.acquire(tracked.requester(), WINDOW_START)


async def test_counters_expire_with_their_window(tracked: Tracked) -> None:
    uploads = limiter(tracked)
    requester = tracked.requester()
    await uploads.acquire(requester, WINDOW_START)

    client_ttl = await tracked.pool.ttl(
        window_key(CLIENT_KEY_PREFIX, requester.client_id, WINDOW_START, WINDOW_SECONDS)
    )
    address_ttl = await tracked.pool.ttl(
        window_key(ADDRESS_KEY_PREFIX, address_bucket(requester.address), WINDOW_START, WINDOW_SECONDS)
    )

    assert 0 < client_ttl <= WINDOW_SECONDS
    assert 0 < address_ttl <= WINDOW_SECONDS


async def test_concurrent_uploads_never_exceed_the_client_limit(tracked: Tracked) -> None:
    uploads = limiter(tracked)
    requester = tracked.requester()

    outcomes = await asyncio.gather(
        *(uploads.acquire(requester, WINDOW_START) for _ in range(PER_CLIENT * 4)), return_exceptions=True
    )

    assert sum(1 for outcome in outcomes if outcome is None) == PER_CLIENT
    assert all(isinstance(outcome, RateLimitedError) for outcome in outcomes if outcome is not None)


async def test_unreachable_redis_is_an_upstream_outage_not_a_free_pass() -> None:
    unreachable = ArqRedis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    try:
        with pytest.raises(UpstreamUnavailableError):
            await RedisUploadLimiter(unreachable, WINDOW, fresh_metrics()).acquire(
                Requester(client_id=uuid4(), address="198.51.100.1"), WINDOW_START
            )
    finally:
        await unreachable.aclose()
