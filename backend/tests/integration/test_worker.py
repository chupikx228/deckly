import asyncio
import contextlib
import dataclasses
import socket
import time
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from arq.connections import ArqRedis, create_pool
from arq.constants import (
    abort_jobs_ss,
    default_queue_name,
    in_progress_key_prefix,
    job_key_prefix,
    result_key_prefix,
)
from arq.worker import Worker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.generations import CancelGeneration
from deckly.application.housekeeping import EnforceJobRetention, SweepStaleJobs
from deckly.application.pipeline import RunGeneration
from deckly.config import Settings
from deckly.domain.job import Cancelled, Failed, FailureCode, GenerationJob, JobStage, JobStatus, Succeeded
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.llm.bounded import BoundedLlmClient
from deckly.infrastructure.llm.client import LlmPrompt
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.media.fetcher import CommonsMediaFetcher
from deckly.infrastructure.moderation.image_judge import LlmCandidateJudge
from deckly.infrastructure.moderation.moderator import LlmContentModerator
from deckly.infrastructure.observability.exposition import METRICS_PATH, serve_metrics
from deckly.infrastructure.resilience import (
    CircuitBreaker,
    CircuitState,
    ResilientCaller,
    RetryPolicy,
    RetryRuntime,
)
from deckly.infrastructure.result_cache import RedisResultCache
from deckly.infrastructure.search.parser import CleaningSourceParser
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.worker.settings import (
    ENFORCE_JOB_RETENTION_KEY,
    LLM_CLIENT_KEY,
    MEDIA_FETCHER_KEY,
    MEDIA_JUDGE_LLM_CLIENT_KEY,
    MODERATION_LLM_CLIENT_KEY,
    OBSERVABILITY_KEY,
    RUN_GENERATION_KEY,
    SETTINGS_KEY,
    SOURCE_RETRIEVER_KEY,
    SWEEP_STALE_JOBS_KEY,
    WorkerContext,
    WorkerSettings,
    from_context,
    shutdown,
    startup,
)
from tests.domain.builders import T0
from tests.fakes import (
    MODEL_THINKING_SECONDS,
    FakeLlmClient,
    FakeProviders,
    InMemoryResultCache,
    SlowModel,
    fresh_observability,
    fresh_probe,
    fresh_telemetry,
    generation_request,
    model_reply,
)
from tests.integration.conftest import Cleanup, job_queue, redis_settings

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PROMPT = LlmPrompt(system="system", user="user", expected_output_tokens=10)
GATE_LIMIT = 20
DEFAULT_JOB_TIMEOUT_SECONDS = 300
SHORT_JOB_TIMEOUT_SECONDS = 0.3
ABORT_POLL_SECONDS = 0.05
WAIT_SECONDS = 10
MAX_OUTPUT_TOKENS = 16_000
PATIENT_POLICY = RetryPolicy(
    max_attempts=1,
    attempt_timeout_seconds=MODEL_THINKING_SECONDS * 10,
    deadline_seconds=MODEL_THINKING_SECONDS * 10,
    base_delay_seconds=1,
    max_delay_seconds=1,
)


async def enqueued_job(store: PostgresJobStore, pool: ArqRedis, cleanup: Cleanup) -> GenerationJob:
    job = GenerationJob.queue(uuid4(), T0)
    cleanup.job_ids.add(job.job_id)
    await store.add(job, generation_request(), cleanup.scope())
    await job_queue(pool).enqueue(job.job_id)
    return job


async def work_off(
    pool: ArqRedis, ctx: WorkerContext, job_id: UUID, *, job_timeout: float = DEFAULT_JOB_TIMEOUT_SECONDS
) -> None:
    score = await pool.zscore(default_queue_name, str(job_id))
    assert score is not None
    worker = Worker(
        functions=WorkerSettings.functions,
        redis_pool=pool,
        ctx=ctx,
        handle_signals=False,
        job_timeout=job_timeout,
    )
    await worker.run_job(str(job_id), int(score))


def context_with(store: PostgresJobStore, providers: FakeProviders) -> WorkerContext:
    return {
        RUN_GENERATION_KEY: RunGeneration(
            store=store,
            cache=InMemoryResultCache(),
            retriever=providers,
            parser=providers,
            generator=providers,
            moderator=providers,
            media=providers,
            clock=utc_now,
            telemetry=fresh_telemetry(),
        )
    }


async def test_job_enqueued_like_the_api_does_is_run_by_the_registered_generation_task(
    session_factory: async_sessionmaker[AsyncSession], queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = await enqueued_job(store, queue_pool, cleanup)
    providers = FakeProviders()

    await work_off(queue_pool, context_with(store, providers), job.job_id)

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Succeeded)
    assert providers.calls == [
        JobStage.RETRIEVING_SOURCES,
        JobStage.PARSING_SOURCES,
        JobStage.GENERATING_CARDS,
    ]
    assert await queue_pool.zscore(default_queue_name, str(job.job_id)) is None
    assert not await queue_pool.exists(job_key_prefix + str(job.job_id), result_key_prefix + str(job.job_id))


async def test_task_of_a_job_cancelled_while_it_waited_in_the_queue_calls_no_port(
    session_factory: async_sessionmaker[AsyncSession], queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = await enqueued_job(store, queue_pool, cleanup)
    await store.update(job.job_id, lambda stored: stored.cancel(utc_now()))
    providers = FakeProviders()

    await work_off(queue_pool, context_with(store, providers), job.job_id)

    stored = await store.get(job.job_id)
    assert stored is not None
    assert stored.status is JobStatus.CANCELLED
    assert providers.calls == []


async def test_worker_startup_wires_the_web_source_retriever_and_parser(
    settings: Settings,
) -> None:
    ctx: WorkerContext = {
        SETTINGS_KEY: settings,
        OBSERVABILITY_KEY: fresh_observability(),
    }

    await startup(ctx)
    try:
        run = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
        retriever = from_context(ctx, SOURCE_RETRIEVER_KEY, WebSourceRetriever)
    finally:
        await shutdown(ctx)

    assert run.retriever is retriever
    assert isinstance(run.parser, CleaningSourceParser)
    assert run.parser.max_characters == settings.providers.search_max_source_characters


async def test_worker_startup_wires_the_commons_media_fetcher_in_place_of_any_stub(
    settings: Settings,
) -> None:
    ctx: WorkerContext = {
        SETTINGS_KEY: settings,
        OBSERVABILITY_KEY: fresh_observability(),
    }

    await startup(ctx)
    try:
        run = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
        media = from_context(ctx, MEDIA_FETCHER_KEY, CommonsMediaFetcher)
    finally:
        await shutdown(ctx)

    assert run.media is media
    assert media.limits.max_images == settings.providers.media_max_images
    assert media.limits.deadline_seconds == settings.providers.media_deadline_seconds


async def test_worker_startup_bounds_content_and_image_moderation_by_one_shared_gate(
    settings: Settings,
) -> None:
    ctx: WorkerContext = {
        SETTINGS_KEY: settings,
        OBSERVABILITY_KEY: fresh_observability(),
    }

    await startup(ctx)
    try:
        run = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
        media = from_context(ctx, MEDIA_FETCHER_KEY, CommonsMediaFetcher)
        moderation_llm = from_context(ctx, MODERATION_LLM_CLIENT_KEY, ResilientLlmClient)
        media_judge_llm = from_context(ctx, MEDIA_JUDGE_LLM_CLIENT_KEY, ResilientLlmClient)
    finally:
        await shutdown(ctx)

    assert isinstance(run.moderator, LlmContentModerator)
    assert isinstance(run.moderator.llm, BoundedLlmClient)
    assert isinstance(media.judge, LlmCandidateJudge)
    assert isinstance(media.judge.llm, BoundedLlmClient)
    assert run.moderator.llm.inner is moderation_llm
    assert media.judge.llm.inner is media_judge_llm
    gate = run.moderator.llm.gate
    assert media.judge.llm.gate is gate
    limit = settings.providers.moderation_max_concurrency
    for _ in range(limit - 1):
        await gate.acquire()
    assert not gate.locked()
    await gate.acquire()
    assert gate.locked()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_worker_exposes_the_moderation_gate_metrics_on_its_metrics_endpoint(
    settings: Settings,
) -> None:
    observability = fresh_observability()
    limited = dataclasses.replace(
        settings,
        providers=settings.providers.model_copy(update={"moderation_max_concurrency": GATE_LIMIT}),
    )
    ctx: WorkerContext = {SETTINGS_KEY: limited, OBSERVABILITY_KEY: observability}
    port = free_port()

    await startup(ctx)
    stop = serve_metrics(observability.metrics, "127.0.0.1", port)
    try:
        run = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
        media = from_context(ctx, MEDIA_FETCHER_KEY, CommonsMediaFetcher)
        assert isinstance(run.moderator, LlmContentModerator)
        assert isinstance(run.moderator.llm, BoundedLlmClient)
        assert isinstance(media.judge, LlmCandidateJudge)
        assert isinstance(media.judge.llm, BoundedLlmClient)
        moderation = dataclasses.replace(run.moderator.llm, inner=FakeLlmClient(model_reply({})))
        images = dataclasses.replace(media.judge.llm, inner=FakeLlmClient(model_reply({})))
        await moderation.complete(PROMPT)
        await images.complete(PROMPT)
        async with httpx2.AsyncClient() as client:
            scraped = (await client.get(f"http://127.0.0.1:{port}{METRICS_PATH}")).text
    finally:
        stop()
        await shutdown(ctx)

    assert 'deckly_gate_in_flight{gate="moderation"} 0.0' in scraped
    assert f'deckly_gate_limit{{gate="moderation"}} {float(GATE_LIMIT)}' in scraped
    assert 'deckly_gate_wait_seconds_count{gate="moderation",outcome="acquired"} 2.0' in scraped
    assert 'deckly_gate_wait_seconds_bucket{gate="moderation",le="0.01",outcome="acquired"} 2.0' in scraped


async def test_worker_startup_wires_the_llm_card_generator_behind_the_resilience_layer(
    settings: Settings,
) -> None:
    ctx: WorkerContext = {
        SETTINGS_KEY: settings,
        OBSERVABILITY_KEY: fresh_observability(),
    }

    await startup(ctx)
    try:
        run = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
        llm = from_context(ctx, LLM_CLIENT_KEY, ResilientLlmClient)
    finally:
        await shutdown(ctx)

    assert isinstance(run.generator, LlmCardGenerator)
    assert run.generator.llm is llm


async def test_worker_startup_wires_the_redis_result_cache_with_the_configured_ttl(
    settings: Settings,
) -> None:
    ctx: WorkerContext = {
        SETTINGS_KEY: settings,
        OBSERVABILITY_KEY: fresh_observability(),
    }

    await startup(ctx)
    try:
        run = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
    finally:
        await shutdown(ctx)

    assert isinstance(run.cache, RedisResultCache)


async def test_worker_startup_wires_housekeeping_with_the_configured_thresholds_and_the_shared_store(
    settings: Settings,
) -> None:
    ctx: WorkerContext = {
        SETTINGS_KEY: settings,
        OBSERVABILITY_KEY: fresh_observability(),
    }

    await startup(ctx)
    try:
        run = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
        sweep = from_context(ctx, SWEEP_STALE_JOBS_KEY, SweepStaleJobs)
        retention = from_context(ctx, ENFORCE_JOB_RETENTION_KEY, EnforceJobRetention)
    finally:
        await shutdown(ctx)

    assert sweep.store is run.store
    assert sweep.housekeeping is retention.housekeeping
    assert sweep.policy.running_after == timedelta(seconds=settings.sweep.running_stale_after_seconds)
    assert sweep.policy.queued_after == timedelta(seconds=settings.sweep.queued_stale_after_seconds)
    assert retention.policy.idempotency_key_ttl == timedelta(
        seconds=settings.cache.idempotency_key_ttl_seconds
    )
    assert retention.policy.job_retention == timedelta(seconds=settings.cache.job_retention_seconds)
    assert sweep.batch_size == retention.batch_size == settings.sweep.batch_size


async def test_job_that_outlives_the_arq_timeout_ends_failed_instead_of_running_forever(
    session_factory: async_sessionmaker[AsyncSession], queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = await enqueued_job(store, queue_pool, cleanup)
    providers = FakeProviders()

    async def hang() -> None:
        await asyncio.Event().wait()

    providers.during[JobStage.GENERATING_CARDS] = hang

    await work_off(
        queue_pool, context_with(store, providers), job.job_id, job_timeout=SHORT_JOB_TIMEOUT_SECONDS
    )

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Failed)
    assert (stored.state.code, stored.stage) == (FailureCode.GENERATION_FAILED, JobStage.GENERATING_CARDS)
    assert await queue_pool.zscore(default_queue_name, str(job.job_id)) is None


def patient_llm(model: SlowModel, breaker: CircuitBreaker) -> ResilientLlmClient:
    runtime = RetryRuntime(clock=time.monotonic, sleep=asyncio.sleep, jitter=lambda: 0.0)
    caller = ResilientCaller(PATIENT_POLICY, breaker, runtime, probe=fresh_probe())
    return ResilientLlmClient(FakeLlmClient(model.think), caller, MAX_OUTPUT_TOKENS)


async def test_cancelling_a_running_job_aborts_its_model_call_inside_the_arq_worker(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    queue_name = f"{default_queue_name}:{uuid4()}"
    pool = await create_pool(redis_settings(settings), default_queue_name=queue_name)
    store = PostgresJobStore(session_factory)
    job = await enqueued_job(store, pool, cleanup)
    model = SlowModel(model_reply({"notes": []}))
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30, clock=time.monotonic, probe=fresh_probe())
    providers = FakeProviders()
    run = RunGeneration(
        store=store,
        cache=InMemoryResultCache(),
        retriever=providers,
        parser=providers,
        generator=LlmCardGenerator(
            llm=patient_llm(model, breaker), new_id=uuid4, handlers=NOTE_TYPE_HANDLERS
        ),
        moderator=providers,
        media=providers,
        clock=utc_now,
        telemetry=fresh_telemetry(),
    )
    worker = Worker(
        functions=WorkerSettings.functions,
        queue_name=queue_name,
        redis_pool=pool,
        ctx={RUN_GENERATION_KEY: run},
        burst=True,
        poll_delay=ABORT_POLL_SECONDS,
        handle_signals=False,
        allow_abort_jobs=WorkerSettings.allow_abort_jobs,
    )
    working = asyncio.create_task(worker.main())
    try:
        await asyncio.wait_for(model.reached.wait(), WAIT_SECONDS)
        await CancelGeneration(
            store=store, queue=job_queue(pool), clock=utc_now, telemetry=fresh_telemetry()
        )(job.job_id)
        await asyncio.wait_for(working, WAIT_SECONDS)
        leftovers = (
            await pool.zscore(queue_name, str(job.job_id)),
            await pool.zscore(abort_jobs_ss, str(job.job_id)),
            await pool.exists(in_progress_key_prefix + str(job.job_id)),
        )
    finally:
        working.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await working
        await pool.delete(queue_name, worker.health_check_key)
        await pool.aclose()

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Cancelled)
    assert stored.stage is JobStage.GENERATING_CARDS
    assert model.endings == ["aborted"]
    assert breaker.state is CircuitState.CLOSED
    assert leftovers == (None, None, 0)
