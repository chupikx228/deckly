import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from ipaddress import IPv6Address, IPv6Network, ip_address
from uuid import UUID

from redis.asyncio import Redis

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.application.ports import Quota, Requester
from deckly.infrastructure.rate_limit import (
    bounded_redis,
    require_count,
    seconds_until_window_end,
    window_end,
    window_key,
)

QUOTA_WINDOW = timedelta(days=1)
QUOTA_WINDOW_SECONDS = int(QUOTA_WINDOW.total_seconds())
CLIENT_KEY_PREFIX = "deckly:generation-quota:client"
ADDRESS_KEY_PREFIX = "deckly:generation-quota:address"
IPV6_BUCKET_PREFIX_LENGTH = 64

ADMITTED = 0
RESERVATION_FIELDS = 2
RESERVE_SCRIPT = """
local client = tonumber(redis.call('GET', KEYS[1]) or '0')
local address = tonumber(redis.call('GET', KEYS[2]) or '0')
if client >= tonumber(ARGV[1]) or address >= tonumber(ARGV[2]) then
    return {1, client}
end
client = redis.call('INCR', KEYS[1])
redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return {0, client}
"""
RELEASE_SCRIPT = """
for _, key in ipairs(KEYS) do
    if tonumber(redis.call('GET', key) or '0') > 0 then
        redis.call('DECR', key)
    end
end
return 0
"""


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    jobs_per_client: int
    jobs_per_address: int
    command_timeout_seconds: float


def address_bucket(address: str) -> str:
    try:
        parsed = ip_address(address)
    except ValueError:
        return address
    if not isinstance(parsed, IPv6Address):
        return str(parsed)
    if parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(IPv6Network((int(parsed), IPV6_BUCKET_PREFIX_LENGTH), strict=False))


def client_key(client_id: UUID, now: datetime) -> str:
    return window_key(CLIENT_KEY_PREFIX, client_id, now, QUOTA_WINDOW_SECONDS)


def address_key(address: str, now: datetime) -> str:
    return window_key(ADDRESS_KEY_PREFIX, address_bucket(address), now, QUOTA_WINDOW_SECONDS)


def quota_from(limit: int, used: int, now: datetime) -> Quota:
    return Quota(limit=limit, remaining=max(0, limit - used), resets_at=window_end(now, QUOTA_WINDOW_SECONDS))


def parse_reservation(answer: object) -> tuple[int, int]:
    if not isinstance(answer, list) or len(answer) != RESERVATION_FIELDS:
        message = f"redis answered the quota reservation with {answer!r}"
        raise TypeError(message)
    outcome, used = answer
    return require_count(outcome, "EVALSHA"), require_count(used, "EVALSHA")


def parse_used(answer: object) -> int:
    if answer is None:
        return 0
    if not isinstance(answer, bytes) or not answer.isdigit():
        message = f"redis answered GET with {answer!r}"
        raise TypeError(message)
    return int(answer)


class RedisGenerationQuota:
    def __init__(self, redis: Redis, limits: QuotaLimits) -> None:
        self._redis = redis
        self._limits = limits
        self._reserve = redis.register_script(RESERVE_SCRIPT)
        self._release = redis.register_script(RELEASE_SCRIPT)

    async def current(self, client_id: UUID, now: datetime) -> Quota:
        async with bounded_redis("generation_quota_unavailable", self._limits.command_timeout_seconds):
            answer = await self._redis.get(client_key(client_id, now))
        return quota_from(self._limits.jobs_per_client, parse_used(answer), now)

    async def reserve(self, requester: Requester, now: datetime) -> Quota:
        async with bounded_redis("generation_quota_unavailable", self._limits.command_timeout_seconds):
            answer = await self._reserve(
                keys=[client_key(requester.client_id, now), address_key(requester.address, now)],
                args=[self._limits.jobs_per_client, self._limits.jobs_per_address, QUOTA_WINDOW_SECONDS],
            )
        outcome, used = parse_reservation(answer)
        if outcome != ADMITTED:
            raise RateLimitedError(seconds_until_window_end(now, QUOTA_WINDOW_SECONDS))
        return quota_from(self._limits.jobs_per_client, used, now)

    async def release(self, requester: Requester, now: datetime) -> None:
        with contextlib.suppress(UpstreamUnavailableError):
            async with bounded_redis("generation_quota_not_released", self._limits.command_timeout_seconds):
                await self._release(
                    keys=[client_key(requester.client_id, now), address_key(requester.address, now)]
                )
