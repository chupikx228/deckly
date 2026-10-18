import asyncio
import sys
from collections.abc import Callable

from arq.connections import ArqRedis
from arq.worker import Worker, create_worker

from deckly.config import TRANSCRIPTION_WORKER_MAX_JOBS, WORKER_MAX_JOBS, Settings, load_settings
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.observability.exposition import serve_metrics
from deckly.infrastructure.observability.runtime import Observability, create_observability
from deckly.infrastructure.queue import TRANSCRIPTION_QUEUE_NAME, create_queue_pool, create_redis_settings
from deckly.worker.settings import (
    OBSERVABILITY_KEY,
    SETTINGS_KEY,
    TranscriptionWorkerSettings,
    WorkerSettings,
    housekeeping_cron_jobs,
)

WORKER_SERVICE_NAME = "deckly-worker"
TRANSCRIPTION_WORKER_SERVICE_NAME = "deckly-transcription-worker"

type WorkerBuilder = Callable[[Settings, Observability, ArqRedis], Worker]


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
        max_jobs=WORKER_MAX_JOBS,
        cron_jobs=housekeeping_cron_jobs(settings.sweep),
        ctx={SETTINGS_KEY: settings, OBSERVABILITY_KEY: observability},
    )


def build_transcription_worker(settings: Settings, observability: Observability, pool: ArqRedis) -> Worker:
    return create_worker(
        TranscriptionWorkerSettings,
        redis_pool=pool,
        queue_name=TRANSCRIPTION_QUEUE_NAME,
        job_timeout=settings.transcription.job_timeout_seconds,
        max_jobs=TRANSCRIPTION_WORKER_MAX_JOBS,
        ctx={SETTINGS_KEY: settings, OBSERVABILITY_KEY: observability},
    )


def run(service_name: str, metrics_port: Callable[[Settings], int], build: WorkerBuilder) -> None:
    settings = load_settings()
    configure_logging(settings.app.log_level)
    observability = create_observability(service_name, settings.observability.trace_exporter, sys.stdout)
    stop_metrics = serve_metrics(
        observability.metrics, settings.observability.worker_metrics_host, metrics_port(settings)
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pool = loop.run_until_complete(connect_queue(settings))
        build(settings, observability, pool).run()
    finally:
        stop_metrics()
        observability.shutdown()
        loop.close()


def generation_metrics_port(settings: Settings) -> int:
    return settings.observability.worker_metrics_port


def transcription_metrics_port(settings: Settings) -> int:
    return settings.observability.transcription_worker_metrics_port


def main() -> None:
    run(WORKER_SERVICE_NAME, generation_metrics_port, build_worker)


def transcription_main() -> None:
    run(TRANSCRIPTION_WORKER_SERVICE_NAME, transcription_metrics_port, build_transcription_worker)


if __name__ == "__main__":
    main()
