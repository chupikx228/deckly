import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.documents import GetDocument, UploadDocument
from deckly.application.generations import CancelGeneration, CreateGeneration, GetGeneration
from deckly.application.health import CheckHealth
from deckly.application.ports import RegenerationLimiter
from deckly.application.regeneration import RegenerateNote
from deckly.config import ProviderSettings, Settings, load_settings
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.card_generator.regenerator import LlmNoteRegenerator
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory, verify_connection
from deckly.infrastructure.documents.sniffing import ContentDocumentInspector
from deckly.infrastructure.documents.store import BoundedDocumentStore, PostgresDocumentStore
from deckly.infrastructure.health import postgres_probe, redis_probe
from deckly.infrastructure.job_store import BoundedJobStore, PostgresJobStore
from deckly.infrastructure.llm.client import LlmClient, LlmEndpoint
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.logging import configure_logging
from deckly.infrastructure.moderation.moderator import (
    LlmContentModerator,
    LlmRegenerationModerator,
    LlmTopicModerator,
)
from deckly.infrastructure.observability.exposition import (
    METRICS_CONTENT_TYPE,
    METRICS_PATH,
    MetricsExposition,
)
from deckly.infrastructure.observability.http import ObservedRequests
from deckly.infrastructure.observability.metrics import DOCUMENT_UPLOAD_GATE
from deckly.infrastructure.observability.runtime import Observability, create_observability
from deckly.infrastructure.provider_faults import UpstreamFaultRegenerator, UpstreamFaultRetriever
from deckly.infrastructure.queue import ArqJobQueue, create_queue_pool, create_redis_settings
from deckly.infrastructure.quota import QuotaLimits, RedisGenerationQuota
from deckly.infrastructure.rate_limit import RedisRegenerationLimiter, RegenerationWindow
from deckly.infrastructure.resilience import ProviderOperation, ProviderProbe, RetryPolicy
from deckly.infrastructure.search.client import SearchClient, SearchEndpoint
from deckly.infrastructure.search.parser import CleaningSourceParser
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from deckly.infrastructure.storage.client import StorageClient
from deckly.infrastructure.upload_gate import RejectingUploadGate
from deckly.infrastructure.upload_limit import RedisUploadLimiter, UploadWindow
from deckly.transport import documents, generations, health, notes
from deckly.transport.dependencies import (
    CANCEL_GENERATION_STATE,
    CHECK_HEALTH_STATE,
    CREATE_GENERATION_STATE,
    GET_DOCUMENT_STATE,
    GET_GENERATION_STATE,
    REGENERATE_NOTE_STATE,
    UPLOAD_DOCUMENT_STATE,
)
from deckly.transport.error_handlers import register_error_handlers
from deckly.worker.settings import (
    LLM_CLIENTS,
    build_document_storage,
    build_storage_client,
    moderation_llm_client,
    resilient_caller,
)

API_PREFIX = "/v1"
SERVICE_TITLE = "Deckly generation service"
ROUTERS: tuple[APIRouter, ...] = (generations.router, notes.router, documents.router, health.router)
SINGLE_ATTEMPT = 1
API_SERVICE_NAME = "deckly-api"
METRICS_EXPOSITION_STATE = "metrics_exposition"

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


@dataclass(frozen=True, slots=True)
class RegenerationClients:
    llm: LlmClient
    search: SearchClient
    moderation_llm: LlmClient
    limiter: RegenerationLimiter
    observability: Observability


@dataclass(frozen=True, slots=True)
class DocumentClients:
    storage: StorageClient
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    observability: Observability


def wire_documents(app: FastAPI, settings: Settings, clients: DocumentClients) -> None:
    store = BoundedDocumentStore(
        PostgresDocumentStore(clients.session_factory),
        timeout_seconds=settings.database.document_store_timeout_seconds,
    )
    limits = settings.limits
    window = UploadWindow(
        uploads_per_client=limits.document_uploads_per_window,
        uploads_per_address=limits.document_uploads_per_address_per_window,
        window_seconds=limits.document_upload_window_seconds,
        command_timeout_seconds=settings.redis.connect_timeout_seconds,
    )
    metrics = clients.observability.metrics
    metrics.gate_limit.labels(gate=DOCUMENT_UPLOAD_GATE).set(limits.document_upload_max_concurrency)
    setattr(
        app.state,
        UPLOAD_DOCUMENT_STATE,
        UploadDocument(
            gate=RejectingUploadGate(
                asyncio.Semaphore(limits.document_upload_max_concurrency), DOCUMENT_UPLOAD_GATE, metrics
            ),
            limiter=RedisUploadLimiter(clients.redis, window, metrics),
            inspector=ContentDocumentInspector(),
            storage=build_document_storage(settings.providers, clients.storage, clients.observability),
            store=store,
            clock=utc_now,
            new_document_id=uuid4,
            max_bytes=limits.document_max_bytes,
            read_timeout_seconds=limits.document_upload_read_timeout_seconds,
        ),
    )
    setattr(app.state, GET_DOCUMENT_STATE, GetDocument(store=store))


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


def single_attempt_moderation(
    providers: ProviderSettings, llm: LlmClient, timeout_seconds: float, probe: ProviderProbe
) -> ResilientLlmClient:
    caller = resilient_caller(
        single_attempt(
            timeout_seconds,
            base_delay_seconds=providers.model_retry_base_delay_seconds,
            max_delay_seconds=providers.model_retry_max_delay_seconds,
        ),
        probe,
        failure_threshold=providers.model_circuit_failure_threshold,
        reset_seconds=providers.model_circuit_reset_seconds,
    )
    return ResilientLlmClient(llm, caller, providers.moderation_max_output_tokens)


def build_topic_moderator(
    settings: Settings, llm: LlmClient, observability: Observability
) -> LlmTopicModerator:
    providers = settings.providers
    return LlmTopicModerator(
        llm=single_attempt_moderation(
            providers,
            llm,
            providers.moderation_timeout_seconds,
            observability.probe(ProviderOperation.TOPIC_MODERATION),
        )
    )


def build_regeneration_moderator(
    settings: Settings, clients: RegenerationClients
) -> LlmRegenerationModerator:
    regenerate = settings.regenerate
    probe = clients.observability.probe
    return LlmRegenerationModerator(
        llm=single_attempt_moderation(
            settings.providers,
            clients.moderation_llm,
            regenerate.request_moderation_timeout_seconds,
            probe(ProviderOperation.REGENERATION_REQUEST_MODERATION),
        ),
        content=LlmContentModerator(
            llm=single_attempt_moderation(
                settings.providers,
                clients.moderation_llm,
                regenerate.note_moderation_timeout_seconds,
                probe(ProviderOperation.REGENERATION_NOTE_MODERATION),
            )
        ),
    )


def regeneration_moderation_llm_client(settings: Settings) -> LlmClient:
    regenerate = settings.regenerate
    return moderation_llm_client(
        settings.providers,
        max(regenerate.request_moderation_timeout_seconds, regenerate.note_moderation_timeout_seconds),
    )


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
        clients.observability.probe(ProviderOperation.REGENERATION_SEARCH),
        failure_threshold=providers.search_circuit_failure_threshold,
        reset_seconds=providers.search_circuit_reset_seconds,
    )
    model_caller = resilient_caller(
        single_attempt(
            regenerate.model_timeout_seconds,
            base_delay_seconds=providers.model_retry_base_delay_seconds,
            max_delay_seconds=providers.model_retry_max_delay_seconds,
        ),
        clients.observability.probe(ProviderOperation.NOTE_REGENERATION),
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
        moderator=build_regeneration_moderator(settings, clients),
        clock=utc_now,
        new_request_id=uuid4,
        timeout_seconds=settings.limits.regenerate_note_timeout_seconds,
    )


async def serve_metrics(request: Request) -> Response:
    exposition: object = getattr(request.app.state, METRICS_EXPOSITION_STATE, None)
    if not isinstance(exposition, MetricsExposition):
        message = f"{METRICS_EXPOSITION_STATE} is not wired into the application state"
        raise TypeError(message)
    return Response(await exposition.render(), media_type=METRICS_CONTENT_TYPE)


def build_lifespan(settings: Settings, observability: Observability) -> Lifespan:
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
                store = BoundedJobStore(
                    PostgresJobStore(session_factory),
                    timeout_seconds=settings.database.job_store_timeout_seconds,
                )
                app.state.settings = settings
                app.state.session_factory = session_factory
                app.state.queue = queue
                jobs = ArqJobQueue(queue, command_timeout_seconds=settings.redis.connect_timeout_seconds)
                quota = RedisGenerationQuota(
                    queue,
                    QuotaLimits(
                        jobs_per_client=settings.limits.generation_jobs_per_day,
                        jobs_per_address=settings.limits.generation_jobs_per_address_per_day,
                        command_timeout_seconds=settings.redis.connect_timeout_seconds,
                    ),
                    observability.metrics,
                )
                setattr(
                    app.state, METRICS_EXPOSITION_STATE, MetricsExposition(observability.metrics, jobs.depth)
                )
                telemetry = observability.generation()
                setattr(
                    app.state,
                    CHECK_HEALTH_STATE,
                    CheckHealth(
                        probes=(
                            postgres_probe(
                                engine, timeout_seconds=settings.database.health_check_timeout_seconds
                            ),
                            redis_probe(queue, timeout_seconds=settings.redis.connect_timeout_seconds),
                        ),
                        quota=quota,
                        clock=utc_now,
                        version=settings.app.version,
                    ),
                )
                setattr(app.state, GET_GENERATION_STATE, GetGeneration(store=store))
                setattr(
                    app.state,
                    CANCEL_GENERATION_STATE,
                    CancelGeneration(store=store, queue=jobs, clock=utc_now, telemetry=telemetry),
                )
                async with AsyncExitStack() as clients:
                    llm = regeneration_llm_client(settings)
                    clients.push_async_callback(llm.aclose)
                    search = regeneration_search_client(settings)
                    clients.push_async_callback(search.aclose)
                    moderation_llm = moderation_llm_client(
                        settings.providers, settings.providers.moderation_timeout_seconds
                    )
                    clients.push_async_callback(moderation_llm.aclose)
                    regeneration_moderation_llm = regeneration_moderation_llm_client(settings)
                    clients.push_async_callback(regeneration_moderation_llm.aclose)
                    storage = await clients.enter_async_context(build_storage_client(settings.providers))
                    wire_documents(
                        app, settings, DocumentClients(storage, session_factory, queue, observability)
                    )
                    setattr(
                        app.state,
                        CREATE_GENERATION_STATE,
                        CreateGeneration(
                            store=store,
                            queue=jobs,
                            quota=quota,
                            moderator=build_topic_moderator(settings, moderation_llm, observability),
                            clock=utc_now,
                            new_job_id=uuid4,
                            telemetry=telemetry,
                        ),
                    )
                    window = RegenerationWindow(
                        limit=settings.limits.note_regenerations_per_window,
                        window_seconds=settings.limits.note_regeneration_window_seconds,
                        command_timeout_seconds=settings.regenerate.rate_limit_timeout_seconds,
                    )
                    setattr(
                        app.state,
                        REGENERATE_NOTE_STATE,
                        build_regenerate_note(
                            settings,
                            RegenerationClients(
                                llm=llm,
                                search=search,
                                moderation_llm=regeneration_moderation_llm,
                                limiter=RedisRegenerationLimiter(queue, window, observability.metrics),
                                observability=observability,
                            ),
                        ),
                    )
                    yield
            finally:
                await queue.aclose()
        finally:
            await engine.dispose()
            observability.shutdown()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    configure_logging(resolved.app.log_level)
    observability = create_observability(API_SERVICE_NAME, resolved.observability.trace_exporter, sys.stdout)
    app = FastAPI(
        title=SERVICE_TITLE, version=resolved.app.version, lifespan=build_lifespan(resolved, observability)
    )
    register_error_handlers(app, str(resolved.app.problem_type_base_url))
    for router in ROUTERS:
        app.include_router(router, prefix=API_PREFIX)
    app.add_api_route(METRICS_PATH, serve_metrics, include_in_schema=False)
    app.add_middleware(ObservedRequests, tracer=observability.tracer, quiet_paths=frozenset({METRICS_PATH}))
    return app
