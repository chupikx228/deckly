import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from secrets import SystemRandom
from uuid import UUID, uuid4

from arq.cron import CronJob, cron
from arq.typing import StartupShutdown, WorkerCoroutine
from arq.worker import Function, func
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from deckly.application.housekeeping import (
    DocumentRetentionPolicy,
    EnforceDocumentRetention,
    EnforceJobRetention,
    RetentionPolicy,
    StalenessPolicy,
    SweepStaleJobs,
)
from deckly.application.pipeline import DocumentSourcing, RunGeneration
from deckly.application.ports import DocumentParser
from deckly.config import (
    MINUTES_PER_HOUR,
    SECONDS_PER_MINUTE,
    ModelProvider,
    ProviderSettings,
    Settings,
    SweepSettings,
)
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory, verify_connection
from deckly.infrastructure.documents.parser import DocumentTextParser
from deckly.infrastructure.documents.store import (
    BoundedDocumentStore,
    PostgresDocumentHousekeeping,
    PostgresDocumentStore,
)
from deckly.infrastructure.job_store import BoundedJobStore, PostgresJobHousekeeping, PostgresJobStore
from deckly.infrastructure.llm.anthropic_client import AnthropicLlmClient
from deckly.infrastructure.llm.bounded import BoundedLlmClient
from deckly.infrastructure.llm.client import LlmClient, LlmEndpoint
from deckly.infrastructure.llm.deepseek_client import DeepSeekLlmClient
from deckly.infrastructure.llm.gemini_client import GeminiLlmClient
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.media.client import MediaEndpoint
from deckly.infrastructure.media.commons_client import CommonsImageSearchClient
from deckly.infrastructure.media.fetcher import CommonsMediaFetcher, MediaLimits
from deckly.infrastructure.moderation.image_judge import LlmCandidateJudge
from deckly.infrastructure.moderation.moderator import LlmContentModerator, LlmMaterialModerator
from deckly.infrastructure.observability.metrics import MODERATION_GATE, Metrics
from deckly.infrastructure.observability.runtime import Observability
from deckly.infrastructure.observability.tracing import continued_trace, trace_carrier_from
from deckly.infrastructure.queue import GENERATION_TASK, create_bounded_pool, create_redis_settings
from deckly.infrastructure.resilience import (
    CircuitBreaker,
    ProviderOperation,
    ProviderProbe,
    ResilientCaller,
    RetryPolicy,
    RetryRuntime,
)
from deckly.infrastructure.result_cache import RedisResultCache, ResultCacheLimits
from deckly.infrastructure.search.client import SearchEndpoint
from deckly.infrastructure.search.parser import CleaningSourceParser
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from deckly.infrastructure.storage.client import StorageClient, StorageEndpoint
from deckly.infrastructure.storage.s3_client import S3StorageClient
from deckly.infrastructure.storage.storage import ResilientDocumentStorage
from deckly.infrastructure.transcription.cache import RedisTranscriptCache, TranscriptCacheLimits
from deckly.infrastructure.transcription.decoding import FfmpegAudioDecoder
from deckly.infrastructure.transcription.model import pinned_checksums, require_ffmpeg, require_model
from deckly.infrastructure.transcription.self_hosted import EngineInvocation, SelfHostedTranscriptionClient
from deckly.infrastructure.transcription.transcriber import RecordingTranscriber

SETTINGS_KEY = "settings"
OBSERVABILITY_KEY = "observability"
CACHE_REDIS_KEY = "cache_redis"
ENGINE_KEY = "engine"
LLM_CLIENT_KEY = "llm_client"
MODERATION_LLM_CLIENT_KEY = "moderation_llm_client"
MEDIA_JUDGE_LLM_CLIENT_KEY = "media_judge_llm_client"
DOCUMENT_MODERATION_LLM_CLIENT_KEY = "document_moderation_llm_client"
SOURCE_RETRIEVER_KEY = "source_retriever"
MEDIA_FETCHER_KEY = "media_fetcher"
RUN_GENERATION_KEY = "run_generation_use_case"
SWEEP_STALE_JOBS_KEY = "sweep_stale_jobs_use_case"
ENFORCE_JOB_RETENTION_KEY = "enforce_job_retention_use_case"
ENFORCE_DOCUMENT_RETENTION_KEY = "enforce_document_retention_use_case"
STORAGE_CLIENT_KEY = "storage_client"
TRANSCRIPTION_CLIENT_KEY = "transcription_client"
HOUSEKEEPING_TASK = "run_housekeeping"

logger = logging.getLogger(__name__)

LLM_CLIENTS: Mapping[ModelProvider, Callable[[LlmEndpoint], LlmClient]] = {
    "anthropic": AnthropicLlmClient,
    "deepseek": DeepSeekLlmClient,
    "gemini": GeminiLlmClient,
}

type WorkerContext = dict[str, object]


def from_context[T](ctx: WorkerContext, key: str, kind: type[T]) -> T:
    value = ctx.get(key)
    if not isinstance(value, kind):
        message = f"{key} is not wired into the worker context"
        raise TypeError(message)
    return value


def resilient_caller(
    policy: RetryPolicy, probe: ProviderProbe, *, failure_threshold: int, reset_seconds: int
) -> ResilientCaller:
    breaker = CircuitBreaker(
        failure_threshold=failure_threshold, reset_seconds=reset_seconds, clock=time.monotonic, probe=probe
    )
    runtime = RetryRuntime(clock=time.monotonic, sleep=asyncio.sleep, jitter=SystemRandom().random)
    return ResilientCaller(policy, breaker, runtime, probe)


def build_llm_client(providers: ProviderSettings, probe: ProviderProbe) -> ResilientLlmClient:
    endpoint = LlmEndpoint(
        base_url=str(providers.model_base_url),
        api_key=providers.model_api_key.get_secret_value(),
        model=providers.model_name,
        max_output_tokens=providers.model_max_output_tokens,
        timeout_seconds=providers.model_timeout_seconds,
    )
    policy = RetryPolicy(
        max_attempts=providers.model_max_attempts,
        attempt_timeout_seconds=providers.model_timeout_seconds,
        deadline_seconds=providers.model_deadline_seconds,
        base_delay_seconds=providers.model_retry_base_delay_seconds,
        max_delay_seconds=providers.model_retry_max_delay_seconds,
    )
    caller = resilient_caller(
        policy,
        probe,
        failure_threshold=providers.model_circuit_failure_threshold,
        reset_seconds=providers.model_circuit_reset_seconds,
    )
    client = LLM_CLIENTS[providers.model_provider](endpoint)
    return ResilientLlmClient(client, caller, providers.model_max_output_tokens)


def moderation_llm_client(providers: ProviderSettings, timeout_seconds: float) -> LlmClient:
    endpoint = LlmEndpoint(
        base_url=str(providers.model_base_url),
        api_key=providers.model_api_key.get_secret_value(),
        model=providers.moderation_model_name,
        max_output_tokens=providers.moderation_max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
    return LLM_CLIENTS[providers.model_provider](endpoint)


def moderation_retry_policy(
    providers: ProviderSettings, *, max_attempts: int, timeout_seconds: int, deadline_seconds: int
) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        attempt_timeout_seconds=timeout_seconds,
        deadline_seconds=deadline_seconds,
        base_delay_seconds=providers.model_retry_base_delay_seconds,
        max_delay_seconds=providers.model_retry_max_delay_seconds,
    )


def resilient_moderation_llm_client(
    providers: ProviderSettings, probe: ProviderProbe, policy: RetryPolicy
) -> ResilientLlmClient:
    caller = resilient_caller(
        policy,
        probe,
        failure_threshold=providers.model_circuit_failure_threshold,
        reset_seconds=providers.model_circuit_reset_seconds,
    )
    client = moderation_llm_client(providers, policy.attempt_timeout_seconds)
    return ResilientLlmClient(client, caller, providers.moderation_max_output_tokens)


def build_moderation_llm_client(providers: ProviderSettings, probe: ProviderProbe) -> ResilientLlmClient:
    policy = moderation_retry_policy(
        providers,
        max_attempts=providers.moderation_filter_max_attempts,
        timeout_seconds=providers.moderation_filter_timeout_seconds,
        deadline_seconds=providers.moderation_filter_deadline_seconds,
    )
    return resilient_moderation_llm_client(providers, probe, policy)


def build_media_judge_llm_client(providers: ProviderSettings, probe: ProviderProbe) -> ResilientLlmClient:
    policy = moderation_retry_policy(
        providers,
        max_attempts=providers.media_judge_max_attempts,
        timeout_seconds=providers.media_judge_timeout_seconds,
        deadline_seconds=providers.media_judge_deadline_seconds,
    )
    return resilient_moderation_llm_client(providers, probe, policy)


def build_source_retriever(providers: ProviderSettings, probe: ProviderProbe) -> WebSourceRetriever:
    endpoint = SearchEndpoint(
        base_url=str(providers.search_base_url),
        api_key=providers.search_api_key.get_secret_value(),
        timeout_seconds=providers.search_timeout_seconds,
    )
    policy = RetryPolicy(
        max_attempts=providers.search_max_attempts,
        attempt_timeout_seconds=providers.search_timeout_seconds,
        deadline_seconds=providers.search_deadline_seconds,
        base_delay_seconds=providers.search_retry_base_delay_seconds,
        max_delay_seconds=providers.search_retry_max_delay_seconds,
    )
    caller = resilient_caller(
        policy,
        probe,
        failure_threshold=providers.search_circuit_failure_threshold,
        reset_seconds=providers.search_circuit_reset_seconds,
    )
    return WebSourceRetriever(
        client=TavilySearchClient(endpoint),
        caller=caller,
        clock=utc_now,
        max_results=providers.search_max_results,
    )


def build_media_fetcher(
    providers: ProviderSettings, probe: ProviderProbe, judge_llm: LlmClient
) -> CommonsMediaFetcher:
    endpoint = MediaEndpoint(
        base_url=str(providers.media_base_url),
        user_agent=providers.media_user_agent,
        timeout_seconds=providers.media_timeout_seconds,
    )
    policy = RetryPolicy(
        max_attempts=providers.media_max_attempts,
        attempt_timeout_seconds=providers.media_timeout_seconds,
        deadline_seconds=providers.media_deadline_seconds,
        base_delay_seconds=providers.media_retry_base_delay_seconds,
        max_delay_seconds=providers.media_retry_max_delay_seconds,
    )
    caller = resilient_caller(
        policy,
        probe,
        failure_threshold=providers.media_circuit_failure_threshold,
        reset_seconds=providers.media_circuit_reset_seconds,
    )
    limits = MediaLimits(
        max_images=providers.media_max_images,
        candidates_per_query=providers.media_candidates_per_query,
        thumbnail_width=providers.media_thumbnail_width,
        max_concurrency=providers.media_max_concurrency,
        deadline_seconds=providers.media_deadline_seconds,
        judged_candidates_per_note=providers.media_judged_candidates_per_note,
    )
    return CommonsMediaFetcher(
        client=CommonsImageSearchClient(endpoint),
        caller=caller,
        judge=LlmCandidateJudge(llm=judge_llm),
        new_id=uuid4,
        limits=limits,
    )


def build_storage_client(providers: ProviderSettings) -> S3StorageClient:
    return S3StorageClient(
        StorageEndpoint(
            endpoint_url=str(providers.storage_endpoint_url),
            bucket=providers.storage_bucket,
            region=providers.storage_region,
            access_key_id=providers.storage_access_key_id,
            secret_access_key=providers.storage_secret_access_key.get_secret_value(),
            timeout_seconds=max(
                providers.storage_upload_timeout_seconds,
                providers.storage_delete_timeout_seconds,
                providers.storage_download_timeout_seconds,
            ),
        )
    )


def storage_policy(
    providers: ProviderSettings, *, timeout_seconds: int, deadline_seconds: int
) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=providers.storage_max_attempts,
        attempt_timeout_seconds=timeout_seconds,
        deadline_seconds=deadline_seconds,
        base_delay_seconds=providers.storage_retry_base_delay_seconds,
        max_delay_seconds=providers.storage_retry_max_delay_seconds,
    )


def storage_caller(providers: ProviderSettings, policy: RetryPolicy, probe: ProviderProbe) -> ResilientCaller:
    return resilient_caller(
        policy,
        probe,
        failure_threshold=providers.storage_circuit_failure_threshold,
        reset_seconds=providers.storage_circuit_reset_seconds,
    )


def build_document_storage(
    providers: ProviderSettings, client: StorageClient, observability: Observability
) -> ResilientDocumentStorage:
    return ResilientDocumentStorage(
        client=client,
        upload_caller=storage_caller(
            providers,
            storage_policy(
                providers,
                timeout_seconds=providers.storage_upload_timeout_seconds,
                deadline_seconds=providers.storage_upload_deadline_seconds,
            ),
            observability.probe(ProviderOperation.DOCUMENT_UPLOAD),
        ),
        delete_caller=storage_caller(
            providers,
            storage_policy(
                providers,
                timeout_seconds=providers.storage_delete_timeout_seconds,
                deadline_seconds=providers.storage_delete_deadline_seconds,
            ),
            observability.probe(ProviderOperation.DOCUMENT_DELETE),
        ),
        download_caller=storage_caller(
            providers,
            storage_policy(
                providers,
                timeout_seconds=providers.storage_download_timeout_seconds,
                deadline_seconds=providers.storage_download_deadline_seconds,
            ),
            observability.probe(ProviderOperation.DOCUMENT_DOWNLOAD),
        ),
    )


def build_cache_redis(settings: Settings) -> Redis:
    return create_bounded_pool(
        create_redis_settings(
            str(settings.cache_redis_url),
            connect_timeout_seconds=settings.redis.connect_timeout_seconds,
            connect_retries=settings.redis.connect_retries,
        ),
        read_timeout_seconds=settings.redis.connect_timeout_seconds,
    )


def build_result_cache(redis: Redis, settings: Settings, metrics: Metrics) -> RedisResultCache:
    return RedisResultCache(
        redis,
        ResultCacheLimits(
            ttl_seconds=settings.cache.generation_result_ttl_seconds,
            command_timeout_seconds=settings.redis.connect_timeout_seconds,
        ),
        metrics,
    )


def build_transcript_cache(redis: Redis, settings: Settings, metrics: Metrics) -> RedisTranscriptCache:
    return RedisTranscriptCache(
        redis,
        TranscriptCacheLimits(
            ttl_seconds=settings.cache.transcript_ttl_seconds,
            command_timeout_seconds=settings.redis.connect_timeout_seconds,
        ),
        metrics,
    )


def build_sweep(
    store: BoundedJobStore, housekeeping: PostgresJobHousekeeping, settings: Settings
) -> SweepStaleJobs:
    return SweepStaleJobs(
        store=store,
        housekeeping=housekeeping,
        policy=StalenessPolicy(
            running_after=timedelta(seconds=settings.sweep.running_stale_after_seconds),
            queued_after=timedelta(seconds=settings.sweep.queued_stale_after_seconds),
            recording_queued_after=timedelta(seconds=settings.sweep.recording_queued_stale_after_seconds),
        ),
        batch_size=settings.sweep.batch_size,
        clock=utc_now,
    )


def build_retention(housekeeping: PostgresJobHousekeeping, settings: Settings) -> EnforceJobRetention:
    return EnforceJobRetention(
        housekeeping=housekeeping,
        policy=RetentionPolicy(
            idempotency_key_ttl=timedelta(seconds=settings.cache.idempotency_key_ttl_seconds),
            job_retention=timedelta(seconds=settings.cache.job_retention_seconds),
        ),
        batch_size=settings.sweep.batch_size,
        clock=utc_now,
    )


def build_document_retention(
    housekeeping: PostgresDocumentHousekeeping, storage: ResilientDocumentStorage, settings: Settings
) -> EnforceDocumentRetention:
    return EnforceDocumentRetention(
        housekeeping=housekeeping,
        storage=storage,
        policy=DocumentRetentionPolicy(
            retention=timedelta(seconds=settings.cache.document_retention_seconds),
            batch_size=settings.sweep.document_batch_size,
            max_concurrency=settings.providers.storage_max_concurrency,
        ),
        clock=utc_now,
    )


def housekeeping_cron_jobs(sweep: SweepSettings) -> tuple[CronJob, ...]:
    return (
        cron(
            run_housekeeping,
            name=HOUSEKEEPING_TASK,
            minute=set(range(0, MINUTES_PER_HOUR, sweep.interval_minutes)),
            timeout=sweep.interval_minutes * SECONDS_PER_MINUTE,
            unique=True,
        ),
    )


async def run_generation(ctx: WorkerContext, job_id: str, trace_context: object = None) -> None:
    with continued_trace(trace_carrier_from(trace_context)):
        await from_context(ctx, RUN_GENERATION_KEY, RunGeneration)(UUID(job_id))


async def run_housekeeping(ctx: WorkerContext) -> None:
    steps: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
        (SWEEP_STALE_JOBS_KEY, from_context(ctx, SWEEP_STALE_JOBS_KEY, SweepStaleJobs)),
        (ENFORCE_JOB_RETENTION_KEY, from_context(ctx, ENFORCE_JOB_RETENTION_KEY, EnforceJobRetention)),
        (
            ENFORCE_DOCUMENT_RETENTION_KEY,
            from_context(ctx, ENFORCE_DOCUMENT_RETENTION_KEY, EnforceDocumentRetention),
        ),
    )
    for name, step in steps:
        try:
            await step()
        except Exception:
            logger.exception("housekeeping_step_failed", extra={"step": name})


def build_document_parser(settings: Settings) -> DocumentTextParser:
    return DocumentTextParser(
        max_characters=settings.limits.document_max_source_characters,
        deadline_seconds=settings.limits.document_parse_deadline_seconds,
    )


def build_transcription_client(settings: Settings) -> SelfHostedTranscriptionClient:
    return SelfHostedTranscriptionClient(
        invocation=EngineInvocation(
            model_directory=settings.transcription.model_directory.resolve(),
            cpu_threads=settings.transcription.cpu_threads,
        ),
        deadline_seconds=settings.transcription.deadline_seconds,
    )


@dataclass(frozen=True, slots=True)
class TranscriptionBackend:
    client: SelfHostedTranscriptionClient
    ffmpeg: Path
    cache_redis: Redis


def build_recording_transcriber(
    settings: Settings, backend: TranscriptionBackend, metrics: Metrics
) -> RecordingTranscriber:
    return RecordingTranscriber(
        decoder=FfmpegAudioDecoder(
            binary=backend.ffmpeg,
            max_seconds=settings.limits.recording_max_duration_seconds,
            deadline_seconds=settings.transcription.decode_deadline_seconds,
        ),
        client=backend.client,
        cache=build_transcript_cache(backend.cache_redis, settings, metrics),
        max_characters=settings.limits.document_max_source_characters,
        metrics=metrics,
    )


async def startup(ctx: WorkerContext) -> None:
    settings = from_context(ctx, SETTINGS_KEY, Settings)
    cache_redis = open_cache_redis(ctx, settings)
    await wire_generation(ctx, build_document_parser(settings), cache_redis)


async def transcription_startup(ctx: WorkerContext) -> None:
    settings = from_context(ctx, SETTINGS_KEY, Settings)
    observability = from_context(ctx, OBSERVABILITY_KEY, Observability)
    if not settings.transcription.enabled:
        message = "the transcription worker cannot start while DECKLY_TRANSCRIPTION_ENABLED is false"
        raise RuntimeError(message)
    ffmpeg = require_ffmpeg()
    require_model(settings.transcription.model_directory, pinned_checksums())
    client = build_transcription_client(settings)
    ctx[TRANSCRIPTION_CLIENT_KEY] = client
    cache_redis = open_cache_redis(ctx, settings)
    backend = TranscriptionBackend(client=client, ffmpeg=ffmpeg, cache_redis=cache_redis)
    transcriber = build_recording_transcriber(settings, backend, observability.metrics)
    await wire_generation(ctx, transcriber, cache_redis)


def open_cache_redis(ctx: WorkerContext, settings: Settings) -> Redis:
    cache_redis = build_cache_redis(settings)
    ctx[CACHE_REDIS_KEY] = cache_redis
    return cache_redis


async def wire_generation(ctx: WorkerContext, parser: DocumentParser, cache_redis: Redis) -> None:
    settings = from_context(ctx, SETTINGS_KEY, Settings)
    observability = from_context(ctx, OBSERVABILITY_KEY, Observability)
    engine = create_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout_seconds=settings.database.pool_timeout_seconds,
    )
    ctx[ENGINE_KEY] = engine
    await verify_connection(engine)
    llm = build_llm_client(settings.providers, observability.probe(ProviderOperation.CARD_GENERATION))
    ctx[LLM_CLIENT_KEY] = llm
    moderation_llm = build_moderation_llm_client(
        settings.providers, observability.probe(ProviderOperation.CONTENT_MODERATION)
    )
    ctx[MODERATION_LLM_CLIENT_KEY] = moderation_llm
    retriever = build_source_retriever(settings.providers, observability.probe(ProviderOperation.WEB_SEARCH))
    ctx[SOURCE_RETRIEVER_KEY] = retriever
    media_judge_llm = build_media_judge_llm_client(
        settings.providers, observability.probe(ProviderOperation.IMAGE_MODERATION)
    )
    ctx[MEDIA_JUDGE_LLM_CLIENT_KEY] = media_judge_llm
    document_moderation_llm = build_moderation_llm_client(
        settings.providers, observability.probe(ProviderOperation.DOCUMENT_MODERATION)
    )
    ctx[DOCUMENT_MODERATION_LLM_CLIENT_KEY] = document_moderation_llm
    moderation_gate = asyncio.Semaphore(settings.providers.moderation_max_concurrency)
    observability.metrics.gate_limit.labels(gate=MODERATION_GATE).set(
        settings.providers.moderation_max_concurrency
    )

    def bounded(inner: LlmClient) -> BoundedLlmClient:
        return BoundedLlmClient(
            inner, moderation_gate, MODERATION_GATE, observability.metrics, time.monotonic
        )

    media = build_media_fetcher(
        settings.providers,
        observability.probe(ProviderOperation.IMAGE_SEARCH),
        bounded(media_judge_llm),
    )
    ctx[MEDIA_FETCHER_KEY] = media
    session_factory = create_session_factory(engine)
    document_retention = timedelta(seconds=settings.cache.document_retention_seconds)
    store = BoundedJobStore(
        PostgresJobStore(session_factory, document_retention=document_retention),
        timeout_seconds=settings.database.job_store_timeout_seconds,
    )
    housekeeping = PostgresJobHousekeeping(session_factory)
    ctx[SWEEP_STALE_JOBS_KEY] = build_sweep(store, housekeeping, settings)
    ctx[ENFORCE_JOB_RETENTION_KEY] = build_retention(housekeeping, settings)
    storage_client = build_storage_client(settings.providers)
    ctx[STORAGE_CLIENT_KEY] = storage_client
    document_storage = build_document_storage(settings.providers, storage_client, observability)
    ctx[ENFORCE_DOCUMENT_RETENTION_KEY] = build_document_retention(
        PostgresDocumentHousekeeping(session_factory), document_storage, settings
    )
    documents = DocumentSourcing(
        store=BoundedDocumentStore(
            PostgresDocumentStore(session_factory, retention=document_retention, clock=utc_now),
            timeout_seconds=settings.database.document_store_timeout_seconds,
        ),
        storage=document_storage,
        parser=parser,
        moderator=LlmMaterialModerator(llm=bounded(document_moderation_llm)),
    )
    ctx[RUN_GENERATION_KEY] = RunGeneration(
        store=store,
        cache=build_result_cache(cache_redis, settings, observability.metrics),
        retriever=retriever,
        parser=CleaningSourceParser(max_characters=settings.providers.search_max_source_characters),
        documents=documents,
        generator=LlmCardGenerator(llm=llm, new_id=uuid4, handlers=NOTE_TYPE_HANDLERS),
        moderator=LlmContentModerator(llm=bounded(moderation_llm)),
        media=media,
        clock=utc_now,
        telemetry=observability.generation(),
    )


async def shutdown(ctx: WorkerContext) -> None:
    engine = ctx.get(ENGINE_KEY)
    cache_redis = ctx.get(CACHE_REDIS_KEY)
    storage_client = ctx.get(STORAGE_CLIENT_KEY)
    transcription_client = ctx.get(TRANSCRIPTION_CLIENT_KEY)
    media = ctx.get(MEDIA_FETCHER_KEY)
    retriever = ctx.get(SOURCE_RETRIEVER_KEY)
    llm_clients = (
        ctx.get(DOCUMENT_MODERATION_LLM_CLIENT_KEY),
        ctx.get(MEDIA_JUDGE_LLM_CLIENT_KEY),
        ctx.get(MODERATION_LLM_CLIENT_KEY),
        ctx.get(LLM_CLIENT_KEY),
    )
    async with AsyncExitStack() as closing:
        if isinstance(engine, AsyncEngine):
            closing.push_async_callback(engine.dispose)
        if isinstance(cache_redis, Redis):
            closing.push_async_callback(cache_redis.aclose)
        if isinstance(storage_client, S3StorageClient):
            closing.push_async_callback(storage_client.aclose)
        if isinstance(transcription_client, SelfHostedTranscriptionClient):
            closing.push_async_callback(transcription_client.aclose)
        if isinstance(media, CommonsMediaFetcher):
            closing.push_async_callback(media.aclose)
        if isinstance(retriever, WebSourceRetriever):
            closing.push_async_callback(retriever.aclose)
        for llm in llm_clients:
            if isinstance(llm, ResilientLlmClient):
                closing.push_async_callback(llm.aclose)


class WorkerSettings:
    functions: Sequence[WorkerCoroutine | Function] = (
        func(run_generation, name=GENERATION_TASK, keep_result=0),
    )
    cron_jobs: Sequence[CronJob] | None = None
    on_startup: StartupShutdown | None = startup
    on_shutdown: StartupShutdown | None = shutdown
    allow_abort_jobs = True


class TranscriptionWorkerSettings:
    functions: Sequence[WorkerCoroutine | Function] = (
        func(run_generation, name=GENERATION_TASK, keep_result=0),
    )
    cron_jobs: Sequence[CronJob] | None = None
    on_startup: StartupShutdown | None = transcription_startup
    on_shutdown: StartupShutdown | None = shutdown
    allow_abort_jobs = True
