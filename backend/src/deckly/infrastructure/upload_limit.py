from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from redis.asyncio import Redis

from deckly.application.exceptions import RateLimitedError
from deckly.application.ports import Requester
from deckly.infrastructure.observability.metrics import Metrics, RateLimit
from deckly.infrastructure.quota import address_bucket
from deckly.infrastructure.rate_limit import (
    bounded_redis,
    record_rejection,
    require_count,
    seconds_until_window_end,
    window_key,
)

CLIENT_KEY_PREFIX = "deckly:document-upload:client"
ADDRESS_KEY_PREFIX = "deckly:document-upload:address"


@dataclass(frozen=True, slots=True)
class UploadWindow:
    uploads_per_client: int
    uploads_per_address: int
    window_seconds: int
    command_timeout_seconds: float


class RedisUploadLimiter:
    def __init__(self, redis: Redis, window: UploadWindow, metrics: Metrics) -> None:
        self._redis = redis
        self._window = window
        self._metrics = metrics

    async def acquire(self, requester: Requester, now: datetime) -> None:
        window_seconds = self._window.window_seconds
        client_key = window_key(CLIENT_KEY_PREFIX, requester.client_id, now, window_seconds)
        address_key = window_key(ADDRESS_KEY_PREFIX, address_bucket(requester.address), now, window_seconds)
        async with (
            bounded_redis("document_upload_limit_unavailable", self._window.command_timeout_seconds),
            self._redis.pipeline(transaction=True) as pipeline,
        ):
            pipeline.incr(client_key)
            pipeline.expire(client_key, window_seconds)
            pipeline.incr(address_key)
            pipeline.expire(address_key, window_seconds)
            client_count, _, address_count, _ = await pipeline.execute()
        if require_count(client_count, "INCR") > self._window.uploads_per_client:
            self._reject(RateLimit.DOCUMENT_UPLOAD_CLIENT, now)
        if require_count(address_count, "INCR") > self._window.uploads_per_address:
            self._reject(RateLimit.DOCUMENT_UPLOAD_ADDRESS, now)

    def _reject(self, limit: RateLimit, now: datetime) -> NoReturn:
        record_rejection(self._metrics, limit)
        raise RateLimitedError(seconds_until_window_end(now, self._window.window_seconds))
