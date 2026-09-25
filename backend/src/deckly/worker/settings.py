from collections.abc import Sequence
from uuid import UUID

from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine
from arq.worker import Function, func
from sqlalchemy.ext.asyncio import AsyncEngine

from deckly.application.pipeline import RunGeneration
from deckly.config import Settings
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory, verify_connection
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.providers import (
    UnimplementedCardGenerator,
    UnimplementedMediaFetcher,
    UnimplementedSourceParser,
    UnimplementedSourceRetriever,
)
from deckly.infrastructure.queue import GENERATION_TASK

SETTINGS_KEY = "settings"
ENGINE_KEY = "engine"
RUN_GENERATION_KEY = "run_generation_use_case"

type WorkerContext = dict[str, object]


def from_context[T](ctx: WorkerContext, key: str, kind: type[T]) -> T:
    value = ctx.get(key)
    if not isinstance(value, kind):
        message = f"{key} is not wired into the worker context"
        raise TypeError(message)
    return value


async def run_generation(ctx: WorkerContext, job_id: str) -> None:
    await from_context(ctx, RUN_GENERATION_KEY, RunGeneration)(UUID(job_id))


async def startup(ctx: WorkerContext) -> None:
    settings = from_context(ctx, SETTINGS_KEY, Settings)
    engine = create_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout_seconds=settings.database.pool_timeout_seconds,
    )
    ctx[ENGINE_KEY] = engine
    await verify_connection(engine)
    ctx[RUN_GENERATION_KEY] = RunGeneration(
        store=PostgresJobStore(create_session_factory(engine)),
        retriever=UnimplementedSourceRetriever(),
        parser=UnimplementedSourceParser(),
        generator=UnimplementedCardGenerator(),
        media=UnimplementedMediaFetcher(),
        clock=utc_now,
    )


async def shutdown(ctx: WorkerContext) -> None:
    engine = ctx.get(ENGINE_KEY)
    if isinstance(engine, AsyncEngine):
        await engine.dispose()


class WorkerSettings:
    functions: Sequence[WorkerCoroutine | Function] = (
        func(run_generation, name=GENERATION_TASK, keep_result=0),
    )
    cron_jobs: Sequence[CronJob] | None = None
    on_startup: StartupShutdown | None = startup
    on_shutdown: StartupShutdown | None = shutdown
