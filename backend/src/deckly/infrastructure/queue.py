from uuid import UUID

from arq.connections import ArqRedis, RedisSettings, create_pool

GENERATION_TASK = "run_generation"


def create_redis_settings(url: str, *, connect_timeout_seconds: int, connect_retries: int) -> RedisSettings:
    settings = RedisSettings.from_dsn(url)
    settings.conn_timeout = connect_timeout_seconds
    settings.conn_retries = connect_retries
    return settings


async def create_queue_pool(settings: RedisSettings) -> ArqRedis:
    return await create_pool(settings)


class ArqJobQueue:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue(self, job_id: UUID) -> None:
        await self._pool.enqueue_job(GENERATION_TASK, str(job_id), _job_id=str(job_id))
