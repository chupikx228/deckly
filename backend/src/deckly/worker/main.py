from arq.worker import run_worker

from deckly.config import load_settings
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.queue import create_redis_settings
from deckly.worker.settings import SETTINGS_KEY, WorkerSettings


def main() -> None:
    settings = load_settings()
    configure_logging(settings.app.log_level)
    run_worker(
        WorkerSettings,
        redis_settings=create_redis_settings(
            str(settings.redis.url),
            connect_timeout_seconds=settings.redis.connect_timeout_seconds,
            connect_retries=settings.redis.connect_retries,
        ),
        job_timeout=settings.limits.generation_job_timeout_seconds,
        ctx={SETTINGS_KEY: settings},
    )


if __name__ == "__main__":
    main()
