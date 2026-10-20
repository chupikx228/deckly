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


RECORDING_URL_CLIENT_KEY_PREFIX = "deckly:recording-url:client"
RECORDING_URL_ADDRESS_KEY_PREFIX = "deckly:recording-url:address"
RECORDING_URL_SERVER_KEY_PREFIX = "deckly:recording-url:server"
ARTICLE_URL_CLIENT_KEY_PREFIX = "deckly:article-url:client"
ARTICLE_URL_ADDRESS_KEY_PREFIX = "deckly:article-url:address"
ARTICLE_URL_SERVER_KEY_PREFIX = "deckly:article-url:server"
SERVER_SUBJECT = "all"


@dataclass(frozen=True, slots=True)
class FetchKeys:
    client_prefix: str
    address_prefix: str
    server_prefix: str
    client_limit: RateLimit
    address_limit: RateLimit
    server_limit: RateLimit
    unavailable_event: str


RECORDING_URL_KEYS = FetchKeys(
    client_prefix=RECORDING_URL_CLIENT_KEY_PREFIX,
    address_prefix=RECORDING_URL_ADDRESS_KEY_PREFIX,
    server_prefix=RECORDING_URL_SERVER_KEY_PREFIX,
    client_limit=RateLimit.RECORDING_URL_CLIENT,
    address_limit=RateLimit.RECORDING_URL_ADDRESS,
    server_limit=RateLimit.RECORDING_URL_SERVER,
    unavailable_event="recording_url_limit_unavailable",
)
ARTICLE_URL_KEYS = FetchKeys(
    client_prefix=ARTICLE_URL_CLIENT_KEY_PREFIX,
    address_prefix=ARTICLE_URL_ADDRESS_KEY_PREFIX,
    server_prefix=ARTICLE_URL_SERVER_KEY_PREFIX,
    client_limit=RateLimit.ARTICLE_URL_CLIENT,
    address_limit=RateLimit.ARTICLE_URL_ADDRESS,
    server_limit=RateLimit.ARTICLE_URL_SERVER,
    unavailable_event="article_url_limit_unavailable",
)


@dataclass(frozen=True, slots=True)
class FetchWindow:
    fetches_per_client: int
    fetches_per_address: int
    fetches_per_server: int
    window_seconds: int
    command_timeout_seconds: float
    keys: FetchKeys = RECORDING_URL_KEYS


class RedisUrlFetchLimiter:
    def __init__(self, redis: Redis, window: FetchWindow, metrics: Metrics) -> None:
        self._redis = redis
        self._window = window
        self._metrics = metrics

    async def acquire(self, requester: Requester, now: datetime) -> None:
        window = self._window
        keys = window.keys
        counted = (
            window_key(keys.client_prefix, requester.client_id, now, window.window_seconds),
            window_key(keys.address_prefix, address_bucket(requester.address), now, window.window_seconds),
            window_key(keys.server_prefix, SERVER_SUBJECT, now, window.window_seconds),
        )
        async with (
            bounded_redis(keys.unavailable_event, window.command_timeout_seconds),
            self._redis.pipeline(transaction=True) as pipeline,
        ):
            for key in counted:
                pipeline.incr(key)
                pipeline.expire(key, window.window_seconds)
            answers = await pipeline.execute()
        client_count, address_count, server_count = (require_count(count, "INCR") for count in answers[::2])
        if client_count > window.fetches_per_client:
            self._reject(keys.client_limit, now)
        if address_count > window.fetches_per_address:
            self._reject(keys.address_limit, now)
        if server_count > window.fetches_per_server:
            self._reject(keys.server_limit, now)

    def _reject(self, limit: RateLimit, now: datetime) -> NoReturn:
        record_rejection(self._metrics, limit)
        raise RateLimitedError(seconds_until_window_end(now, self._window.window_seconds))
