import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from arq.connections import ArqRedis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from deckly.infrastructure.database import verify_connection

logger = logging.getLogger(__name__)

POSTGRES = "postgres"
REDIS = "redis"

type Check = Callable[[], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    check: Check
    failures: tuple[type[Exception], ...]


class BoundedProbe:
    def __init__(self, dependency: Dependency, *, timeout_seconds: float) -> None:
        self._dependency = dependency
        self._timeout_seconds = timeout_seconds
        self._in_flight: asyncio.Task[bool] | None = None

    async def is_healthy(self) -> bool:
        if self._in_flight is None or self._in_flight.done():
            self._in_flight = asyncio.create_task(self._run_check())
        in_flight = self._in_flight
        if in_flight.cancelling():
            return False
        await asyncio.wait({in_flight}, timeout=self._timeout_seconds)
        if not in_flight.done():
            in_flight.cancel()
            logger.warning("dependency_unresponsive", extra={"dependency": self._dependency.name})
            return False
        return not in_flight.cancelled() and in_flight.result()

    async def _run_check(self) -> bool:
        try:
            await self._dependency.check()
        except Exception as error:
            expected = isinstance(error, self._dependency.failures)
            logger.log(
                logging.WARNING if expected else logging.ERROR,
                "dependency_unreachable",
                exc_info=None if expected else error,
                extra={"dependency": self._dependency.name, "error": type(error).__name__},
            )
            return False
        return True


def postgres_probe(engine: AsyncEngine, *, timeout_seconds: float) -> BoundedProbe:
    return BoundedProbe(
        Dependency(
            name=POSTGRES,
            check=partial(verify_connection, engine),
            failures=(SQLAlchemyError, OSError),
        ),
        timeout_seconds=timeout_seconds,
    )


def redis_probe(pool: ArqRedis, *, timeout_seconds: float) -> BoundedProbe:
    return BoundedProbe(
        Dependency(name=REDIS, check=pool.ping, failures=(RedisError, OSError)),
        timeout_seconds=timeout_seconds,
    )
