from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from fastapi import APIRouter, FastAPI

from deckly.application.generations import CancelGeneration, CreateGeneration, GetGeneration
from deckly.application.ports import RegenerationLimiter
from deckly.application.regeneration import RegenerateNote
from deckly.config import Settings, load_settings
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.card_generator.regenerator import LlmNoteRegenerator
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory, verify_connection
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.llm.client import LlmClient, LlmEndpoint
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.provider_faults import UpstreamFaultRegenerator, UpstreamFaultRetriever
from deckly.infrastructure.queue import ArqJobQueue, create_queue_pool, create_redis_settings
from deckly.infrastructure.quota import UnmeteredQuota
from deckly.infrastructure.rate_limit import RedisRegenerationLimiter, RegenerationWindow
from deckly.infrastructure.resilience import RetryPolicy
from deckly.infrastructure.search.client import SearchClient, SearchEndpoint
from deckly.infrastructure.search.parser import CleaningSourceParser
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from deckly.transport import generations, notes
from deckly.transport.error_handlers import register_error_handlers
from deckly.worker.settings import LLM_CLIENTS, resilient_caller

API_PREFIX = "/v1"
SERVICE_TITLE = "Deckly generation service"
ROUTERS: tuple[APIRouter, ...] = (generations.router, notes.router)
SINGLE_ATTEMPT = 1

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


@dataclass(frozen=True, slots=True)
class RegenerationClients:
    llm: LlmClient
    search: SearchClient
    limiter: RegenerationLimiter


def single_attempt(timeout_seconds: float, *, base_delay_seconds: int, max_delay_seconds: int) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=SINGLE_ATTEMPT,
        attempt_timeout_seconds=timeout_seconds,
        deadline_seconds=timeout_seconds,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )


def regeneration_llm_client(settings: Settings) -> LlmClient:
    endpoint = LlmEndpoint(
        base_url=str(settings.providers.model_base_url),
        api_key=settings.providers.model_api_key.get_secret_value(),
        model=settings.providers.model_name,
        max_output_tokens=settings.regenerate.model_max_output_tokens,
        timeout_seconds=settings.regenerate.model_timeout_seconds,
    )
    return LLM_CLIENTS[settings.providers.model_provider](endpoint)


def regeneration_search_client(settings: Settings) -> SearchClient:
    return TavilySearchClient(
        SearchEndpoint(
            base_url=str(settings.providers.search_base_url),
            api_key=settings.providers.search_api_key.get_secret_value(),
            timeout_seconds=settings.regenerate.search_timeout_seconds,
        )
    )


def build_regenerate_note(settings: Settings, clients: RegenerationClients) -> RegenerateNote:
    providers = settings.providers
    regenerate = settings.regenerate
    search_caller = resilient_caller(
        single_attempt(
            regenerate.search_timeout_seconds,
            base_delay_seconds=providers.search_retry_base_delay_seconds,
            max_delay_seconds=providers.search_retry_max_delay_seconds,
        ),
        failure_threshold=providers.search_circuit_failure_threshold,
        reset_seconds=providers.search_circuit_reset_seconds,
    )
    model_caller = resilient_caller(
        single_attempt(
            regenerate.model_timeout_seconds,
            base_delay_seconds=providers.model_retry_base_delay_seconds,
            max_delay_seconds=providers.model_retry_max_delay_seconds,
        ),
        failure_threshold=providers.model_circuit_failure_threshold,
        reset_seconds=providers.model_circuit_reset_seconds,
    )
    return RegenerateNote(
        limiter=clients.limiter,
        retriever=UpstreamFaultRetriever(
            WebSourceRetriever(
                client=clients.search,
                caller=search_caller,
                clock=utc_now,
                max_results=regenerate.search_max_results,
            )
        ),
        parser=CleaningSourceParser(max_characters=regenerate.search_max_source_characters),
        regenerator=UpstreamFaultRegenerator(
            LlmNoteRegenerator(
                llm=ResilientLlmClient(clients.llm, model_caller, regenerate.model_max_output_tokens),
                new_id=uuid4,
                handlers=NOTE_TYPE_HANDLERS,
            )
        ),
        clock=utc_now,
        new_request_id=uuid4,
        timeout_seconds=settings.limits.regenerate_note_timeout_seconds,
    )


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
                llm = regeneration_llm_client(settings)
                try:
                    search = regeneration_search_client(settings)
                    try:
                        window = RegenerationWindow(
                            limit=settings.limits.note_regenerations_per_window,
                            window_seconds=settings.limits.note_regeneration_window_seconds,
                            command_timeout_seconds=settings.regenerate.rate_limit_timeout_seconds,
                        )
                        app.state.regenerate_note = build_regenerate_note(
                            settings,
                            RegenerationClients(
                                llm=llm, search=search, limiter=RedisRegenerationLimiter(queue, window)
                            ),
                        )
                        yield
                    finally:
                        await search.aclose()
                finally:
                    await llm.aclose()
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
