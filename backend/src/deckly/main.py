from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import APIRouter, FastAPI

from deckly.config import Settings, load_settings
from deckly.infrastructure.database import create_engine, create_session_factory, verify_connection
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.queue import create_queue_pool, create_redis_settings
from deckly.transport.error_handlers import register_error_handlers

API_PREFIX = "/v1"
SERVICE_TITLE = "Deckly generation service"
ROUTERS: tuple[APIRouter, ...] = ()

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
                )
            )
            try:
                app.state.settings = settings
                app.state.session_factory = create_session_factory(engine)
                app.state.queue = queue
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
