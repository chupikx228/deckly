from arq.connections import ArqRedis, RedisSettings, create_pool


def create_redis_settings(url: str, *, connect_timeout_seconds: int, connect_retries: int) -> RedisSettings:
    settings = RedisSettings.from_dsn(url)
    settings.conn_timeout = connect_timeout_seconds
    settings.conn_retries = connect_retries
    return settings


async def create_queue_pool(settings: RedisSettings) -> ArqRedis:
    return await create_pool(settings)
