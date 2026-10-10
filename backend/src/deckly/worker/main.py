import asyncio
import sys

from arq.connections import ArqRedis
from arq.worker import Worker, create_worker

from deckly.config import Settings, load_settings
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.observability.exposition import serve_metrics
from deckly.infrastructure.observability.runtime import Observability, create_observability
from deckly.infrastructure.queue import create_queue_pool, create_redis_settings
from deckly.worker.settings import OBSERVABILITY_KEY, SETTINGS_KEY, WorkerSettings, housekeeping_cron_jobs

WORKER_SERVICE_NAME = "deckly-worker"


async def connect_queue(settings: Settings) -> ArqRedis:
    return await create_queue_pool(
        create_redis_settings(
            str(settings.redis.url),
            connect_timeout_seconds=settings.redis.connect_timeout_seconds,
            connect_retries=settings.redis.connect_retries,
        ),
        read_timeout_seconds=settings.redis.connect_timeout_seconds,
    )


def build_worker(settings: Settings, observability: Observability, pool: ArqRedis) -> Worker:
    return create_worker(
        WorkerSettings,
        redis_pool=pool,
        job_timeout=settings.limits.generation_job_timeout_seconds,
        cron_jobs=housekeeping_cron_jobs(settings.sweep),
        ctx={SETTINGS_KEY: settings, OBSERVABILITY_KEY: observability},
    )


def main() -> None:
    settings = load_settings()
    configure_logging(settings.app.log_level)
    observability = create_observability(
        WORKER_SERVICE_NAME, settings.observability.trace_exporter, sys.stdout
    )
    stop_metrics = serve_metrics(
        observability.metrics,
        settings.observability.worker_metrics_host,
        settings.observability.worker_metrics_port,
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pool = loop.run_until_complete(connect_queue(settings))
        build_worker(settings, observability, pool).run()
    finally:
        stop_metrics()
        observability.shutdown()
        loop.close()


if __name__ == "__main__":
    main()
