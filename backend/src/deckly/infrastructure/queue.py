import asyncio
import logging
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings
from arq.constants import abort_jobs_ss
from arq.utils import timestamp_ms
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

GENERATION_TASK = "run_generation"


def create_redis_settings(url: str, *, connect_timeout_seconds: int, connect_retries: int) -> RedisSettings:
    settings = RedisSettings.from_dsn(url)
    settings.conn_timeout = connect_timeout_seconds
    settings.conn_retries = connect_retries
    return settings


async def create_queue_pool(settings: RedisSettings, *, read_timeout_seconds: float) -> ArqRedis:
    pool = ArqRedis(
        host=settings.host,
        port=settings.port,
        unix_socket_path=settings.unix_socket_path,
        db=settings.database,
        username=settings.username,
        password=settings.password,
        ssl=settings.ssl,
        socket_connect_timeout=settings.conn_timeout,
        socket_timeout=read_timeout_seconds,
    )
    retries_left = settings.conn_retries
    while True:
        try:
            await pool.ping()
        except (RedisError, OSError) as error:
            if retries_left == 0:
                await pool.aclose()
                raise
            logger.warning(
                "queue_connection_retrying",
                extra={"retries_left": retries_left, "error": type(error).__name__},
            )
            retries_left -= 1
            await asyncio.sleep(settings.conn_retry_delay)
        else:
            return pool


class ArqJobQueue:
    def __init__(self, pool: ArqRedis, *, command_timeout_seconds: float) -> None:
        self._pool = pool
        self._command_timeout_seconds = command_timeout_seconds

    async def enqueue(self, job_id: UUID) -> None:
        async with asyncio.timeout(self._command_timeout_seconds):
            await self._pool.enqueue_job(GENERATION_TASK, str(job_id), _job_id=str(job_id))

    async def abort(self, job_id: UUID) -> None:
        try:
            async with asyncio.timeout(self._command_timeout_seconds):
                await self._pool.zadd(abort_jobs_ss, {str(job_id): timestamp_ms()})
        except (RedisError, TimeoutError):
            logger.exception("job_abort_not_signalled", extra={"job_id": str(job_id)})
