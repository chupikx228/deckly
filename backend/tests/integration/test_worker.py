import asyncio
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from arq.constants import default_queue_name, job_key_prefix, result_key_prefix
from arq.worker import Worker
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.pipeline import RunGeneration
from deckly.config import Settings
from deckly.domain.job import Failed, FailureCode, GenerationJob, JobStage, JobStatus, Succeeded
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.queue import ArqJobQueue
from deckly.worker.settings import (
    RUN_GENERATION_KEY,
    SETTINGS_KEY,
    WorkerContext,
    WorkerSettings,
    shutdown,
    startup,
)
from tests.domain.builders import T0
from tests.fakes import FakeProviders, generation_request
from tests.integration.conftest import Cleanup

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

DEFAULT_JOB_TIMEOUT_SECONDS = 300
SHORT_JOB_TIMEOUT_SECONDS = 0.3


async def enqueued_job(store: PostgresJobStore, pool: ArqRedis, cleanup: Cleanup) -> GenerationJob:
    job = GenerationJob.queue(uuid4(), T0)
    cleanup.job_ids.add(job.job_id)
    await store.add(job, generation_request(), cleanup.scope())
    await ArqJobQueue(pool).enqueue(job.job_id)
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
            retriever=providers,
            parser=providers,
            generator=providers,
            media=providers,
            clock=utc_now,
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


async def test_worker_started_with_the_stub_adapters_fails_jobs_instead_of_leaving_them_queued(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    queue_pool: ArqRedis,
    cleanup: Cleanup,
) -> None:
    store = PostgresJobStore(session_factory)
    job = await enqueued_job(store, queue_pool, cleanup)
    ctx: WorkerContext = {SETTINGS_KEY: settings}

    await startup(ctx)
    try:
        await work_off(queue_pool, ctx, job.job_id)
    finally:
        await shutdown(ctx)

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Failed)
    assert (stored.state.code, stored.stage) == (FailureCode.GENERATION_FAILED, JobStage.RETRIEVING_SOURCES)


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
