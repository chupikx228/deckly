import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError

logger = logging.getLogger(__name__)

KEY_PREFIX = "deckly:regenerate-note"
MIN_RETRY_AFTER_SECONDS = 1


@dataclass(frozen=True, slots=True)
class RegenerationWindow:
    limit: int
    window_seconds: int
    command_timeout_seconds: float


def window_key(client_id: UUID, now: datetime, window_seconds: int) -> str:
    return f"{KEY_PREFIX}:{client_id}:{math.floor(now.timestamp()) // window_seconds}"


def seconds_until_window_end(now: datetime, window_seconds: int) -> int:
    elapsed = now.timestamp() % window_seconds
    return max(MIN_RETRY_AFTER_SECONDS, math.ceil(window_seconds - elapsed))


class RedisRegenerationLimiter:
    def __init__(self, redis: Redis, window: RegenerationWindow) -> None:
        self._redis = redis
        self._window = window

    async def acquire(self, client_id: UUID, now: datetime) -> None:
        key = window_key(client_id, now, self._window.window_seconds)
        try:
            async with asyncio.timeout(self._window.command_timeout_seconds):
                async with self._redis.pipeline(transaction=True) as pipeline:
                    pipeline.incr(key)
                    pipeline.expire(key, self._window.window_seconds)
                    count, _ = await pipeline.execute()
        except (RedisError, OSError, TimeoutError) as error:
            logger.warning("regeneration_limit_unavailable", extra={"error": type(error).__name__})
            raise UpstreamUnavailableError(MIN_RETRY_AFTER_SECONDS) from error
        if not isinstance(count, int):
            message = f"redis answered INCR with {type(count).__name__}"
            raise TypeError(message)
        if count > self._window.limit:
            raise RateLimitedError(seconds_until_window_end(now, self._window.window_seconds))
