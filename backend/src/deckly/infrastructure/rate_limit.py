import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.infrastructure.observability.metrics import Metrics, RateLimit

logger = logging.getLogger(__name__)

KEY_PREFIX = "deckly:regenerate-note"
MIN_RETRY_AFTER_SECONDS = 1


@dataclass(frozen=True, slots=True)
class RegenerationWindow:
    limit: int
    window_seconds: int
    command_timeout_seconds: float


def window_index(now: datetime, window_seconds: int) -> int:
    return math.floor(now.timestamp()) // window_seconds


def window_key(prefix: str, subject: object, now: datetime, window_seconds: int) -> str:
    return f"{prefix}:{subject}:{window_index(now, window_seconds)}"


def window_end(now: datetime, window_seconds: int) -> datetime:
    return datetime.fromtimestamp((window_index(now, window_seconds) + 1) * window_seconds, UTC)


def seconds_until_window_end(now: datetime, window_seconds: int) -> int:
    elapsed = now.timestamp() % window_seconds
    return max(MIN_RETRY_AFTER_SECONDS, math.ceil(window_seconds - elapsed))


def require_count(answer: object, command: str) -> int:
    if not isinstance(answer, int):
        message = f"redis answered {command} with {type(answer).__name__}"
        raise TypeError(message)
    return answer


def record_rejection(metrics: Metrics, limit: RateLimit) -> None:
    metrics.rate_limit_rejections.labels(limit=limit).inc()
    logger.info("rate_limit_rejected", extra={"limit": limit})


@asynccontextmanager
async def bounded_redis(event: str, timeout_seconds: float) -> AsyncIterator[None]:
    try:
        async with asyncio.timeout(timeout_seconds):
            yield
    except (RedisError, OSError, TimeoutError) as error:
        logger.warning(event, extra={"error": type(error).__name__})
        raise UpstreamUnavailableError(MIN_RETRY_AFTER_SECONDS) from error


class RedisRegenerationLimiter:
    def __init__(self, redis: Redis, window: RegenerationWindow, metrics: Metrics) -> None:
        self._redis = redis
        self._window = window
        self._metrics = metrics

    async def acquire(self, client_id: UUID, now: datetime) -> None:
        key = window_key(KEY_PREFIX, client_id, now, self._window.window_seconds)
        async with (
            bounded_redis("regeneration_limit_unavailable", self._window.command_timeout_seconds),
            self._redis.pipeline(transaction=True) as pipeline,
        ):
            pipeline.incr(key)
            pipeline.expire(key, self._window.window_seconds)
            count, _ = await pipeline.execute()
        if require_count(count, "INCR") > self._window.limit:
            record_rejection(self._metrics, RateLimit.NOTE_REGENERATION)
            raise RateLimitedError(seconds_until_window_end(now, self._window.window_seconds))
