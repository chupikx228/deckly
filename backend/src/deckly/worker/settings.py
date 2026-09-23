from collections.abc import Sequence

from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine
from arq.worker import Function


class WorkerSettings:
    functions: Sequence[WorkerCoroutine | Function] = ()
    cron_jobs: Sequence[CronJob] | None = None
    on_startup: StartupShutdown | None = None
    on_shutdown: StartupShutdown | None = None
