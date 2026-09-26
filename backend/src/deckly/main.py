from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import uuid4

from fastapi import APIRouter, FastAPI

from deckly.application.generations import CancelGeneration, CreateGeneration, GetGeneration
from deckly.config import Settings, load_settings
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory, verify_connection
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.queue import ArqJobQueue, create_queue_pool, create_redis_settings
from deckly.infrastructure.quota import UnmeteredQuota
from deckly.transport import generations
from deckly.transport.error_handlers import register_error_handlers

API_PREFIX = "/v1"
SERVICE_TITLE = "Deckly generation service"
ROUTERS: tuple[APIRouter, ...] = (generations.router,)

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_lifespan(settings: Settings) -> Lifespan:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(
            str(settings.database.url),
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_timeout_seconds=settings.database.pool_timeout_seconds,
        )
        try:
            await verify_connection(engine)
            queue = await create_queue_pool(
                create_redis_settings(
                    str(settings.redis.url),
                    connect_timeout_seconds=settings.redis.connect_timeout_seconds,
                    connect_retries=settings.redis.connect_retries,
                ),
                read_timeout_seconds=settings.redis.connect_timeout_seconds,
            )
            try:
                session_factory = create_session_factory(engine)
                store = PostgresJobStore(session_factory)
                app.state.settings = settings
                app.state.session_factory = session_factory
                app.state.queue = queue
                jobs = ArqJobQueue(queue, command_timeout_seconds=settings.redis.connect_timeout_seconds)
                app.state.create_generation = CreateGeneration(
                    store=store,
                    queue=jobs,
                    quota=UnmeteredQuota(settings.limits.generation_jobs_per_day),
                    clock=utc_now,
                    new_job_id=uuid4,
                )
                app.state.get_generation = GetGeneration(store=store)
                app.state.cancel_generation = CancelGeneration(store=store, queue=jobs, clock=utc_now)
                yield
            finally:
                await queue.aclose()
        finally:
            await engine.dispose()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    configure_logging(resolved.app.log_level)
    app = FastAPI(title=SERVICE_TITLE, version=resolved.app.version, lifespan=build_lifespan(resolved))
    register_error_handlers(app, str(resolved.app.problem_type_base_url))
    for router in ROUTERS:
        app.include_router(router, prefix=API_PREFIX)
    return app
