import sys

from arq.worker import run_worker

from deckly.config import load_settings
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.observability.exposition import serve_metrics
from deckly.infrastructure.observability.runtime import create_observability
from deckly.infrastructure.queue import create_redis_settings
from deckly.worker.settings import OBSERVABILITY_KEY, SETTINGS_KEY, WorkerSettings, housekeeping_cron_jobs

WORKER_SERVICE_NAME = "deckly-worker"


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
    try:
        run_worker(
            WorkerSettings,
            redis_settings=create_redis_settings(
                str(settings.redis.url),
                connect_timeout_seconds=settings.redis.connect_timeout_seconds,
                connect_retries=settings.redis.connect_retries,
            ),
            job_timeout=settings.limits.generation_job_timeout_seconds,
            cron_jobs=housekeeping_cron_jobs(settings.sweep),
            ctx={SETTINGS_KEY: settings, OBSERVABILITY_KEY: observability},
        )
    finally:
        stop_metrics()
        observability.shutdown()


if __name__ == "__main__":
    main()
