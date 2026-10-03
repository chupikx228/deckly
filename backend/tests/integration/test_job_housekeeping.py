import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.housekeeping import (
    EnforceJobRetention,
    RetentionOutcome,
    RetentionPolicy,
    StalenessPolicy,
    SweepStaleJobs,
    fail_if_stale,
)
from deckly.application.pipeline import RunGeneration
from deckly.application.ports import IdempotencyScope
from deckly.config import Settings
from deckly.domain.job import Failed, FailureCode, GenerationJob, JobStage, JobStatus
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure import job_store
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.job_store import PostgresJobHousekeeping, PostgresJobStore
from deckly.infrastructure.tables import GenerationJobRow
from tests.domain.builders import FULL_RESULT
from tests.fakes import FakeProviders, InMemoryResultCache, generation_request
from tests.integration.conftest import Cleanup
from tests.integration.test_postgres_job_store import RowHolder, until_an_update_waits_on_the_row_lock

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)
RUNNING_AFTER = timedelta(minutes=15)
QUEUED_AFTER = timedelta(hours=1)
SWEPT_AT = ANCIENT + timedelta(hours=2)
DAY = timedelta(days=1)
BATCH_SIZE = 1000
SKIP_LOCKED_TIMEOUT_SECONDS = 5


def at(offset: timedelta) -> datetime:
    return ANCIENT + offset


def queued(offset: timedelta = timedelta()) -> GenerationJob:
    return GenerationJob.queue(uuid4(), at(offset))


def running(offset: timedelta = timedelta()) -> GenerationJob:
    return queued().start(at(offset))


async def stored(store: PostgresJobStore, cleanup: Cleanup, job: GenerationJob) -> UUID:
    await store.add(job, generation_request(), cleanup.scope())
    return job.job_id


def sweeper(session_factory: async_sessionmaker[AsyncSession], now: datetime = SWEPT_AT) -> SweepStaleJobs:
    return SweepStaleJobs(
        store=PostgresJobStore(session_factory),
        housekeeping=PostgresJobHousekeeping(session_factory),
        policy=StalenessPolicy(running_after=RUNNING_AFTER, queued_after=QUEUED_AFTER),
        batch_size=BATCH_SIZE,
        clock=lambda: now,
    )


def pipeline(store: PostgresJobStore, providers: FakeProviders) -> RunGeneration:
    return RunGeneration(
        store=store,
        cache=InMemoryResultCache(),
        retriever=providers,
        parser=providers,
        generator=providers,
        moderator=providers,
        media=providers,
        clock=utc_now,
    )


async def invalidate_saved_request(session_factory: async_sessionmaker[AsyncSession], job_id: UUID) -> None:
    async with session_factory.begin() as session:
        await session.execute(
            update(GenerationJobRow)
            .where(GenerationJobRow.job_id == job_id)
            .values(note_types=[NoteType.IMAGE_OCCLUSION], include_images=False)
        )


async def rows_of(
    session_factory: async_sessionmaker[AsyncSession], client_id: UUID
) -> list[GenerationJobRow]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(GenerationJobRow)
            .where(GenerationJobRow.client_id == client_id)
            .order_by(GenerationJobRow.created_at)
        )
        return list(rows)


async def test_stale_lists_jobs_of_one_status_last_changed_before_the_cutoff_oldest_first(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    older = await stored(store, cleanup, running(timedelta(seconds=1)))
    oldest = await stored(store, cleanup, running())
    await stored(store, cleanup, running(DAY))
    await stored(store, cleanup, queued())
    await stored(store, cleanup, running().succeed(FULL_RESULT, ANCIENT))
    housekeeping = PostgresJobHousekeeping(session_factory)

    listed = await housekeeping.stale(JobStatus.RUNNING, at(timedelta(hours=1)), BATCH_SIZE)
    first = await housekeeping.stale(JobStatus.RUNNING, at(timedelta(hours=1)), 1)

    assert [job_id for job_id in listed if job_id in {older, oldest}] == [oldest, older]
    assert first == (oldest,)


async def test_released_key_starts_a_new_job_while_the_original_row_stays_pollable(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    expired_scope, fresh_scope = cleanup.scope(), cleanup.scope()
    original = await store.add(queued(), generation_request(), expired_scope)
    fresh = await store.add(queued(2 * DAY), generation_request(), fresh_scope)

    released = await PostgresJobHousekeeping(session_factory).release_idempotency_keys(at(DAY), BATCH_SIZE)

    assert released >= 1
    assert await store.find(expired_scope) is None
    assert await store.find(fresh_scope) == fresh
    assert await store.get(original.job.job_id) == original.job
    replacement = await store.add(queued(3 * DAY), generation_request(), expired_scope)
    assert replacement.job.job_id != original.job.job_id
    assert await store.find(expired_scope) == replacement
    [kept, added] = await rows_of(session_factory, expired_scope.client_id)
    assert (kept.job_id, kept.idempotency_key) == (original.job.job_id, None)
    assert (added.job_id, added.idempotency_key) == (replacement.job.job_id, expired_scope.idempotency_key)


async def test_purge_deletes_only_finished_jobs_past_retention(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    expired = [
        await stored(store, cleanup, running().succeed(FULL_RESULT, ANCIENT)),
        await stored(store, cleanup, queued().cancel(ANCIENT)),
        await stored(store, cleanup, running().fail(FailureCode.PROVIDER_UNAVAILABLE, ANCIENT)),
    ]
    kept = [
        await stored(store, cleanup, queued()),
        await stored(store, cleanup, running()),
        await stored(store, cleanup, running().succeed(FULL_RESULT, at(2 * DAY))),
    ]

    purged = await PostgresJobHousekeeping(session_factory).purge_finished(at(DAY), BATCH_SIZE)

    assert purged >= len(expired)
    assert [await store.get(job_id) for job_id in expired] == [None, None, None]
    assert None not in [await store.get(job_id) for job_id in kept]


async def test_purge_skips_a_row_someone_holds_instead_of_waiting_for_it(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    held = await stored(store, cleanup, queued().cancel(ANCIENT))
    holder = RowHolder(settings, held)
    holding = asyncio.create_task(holder.update(lambda job: job))
    await holder.until_locked()
    try:
        async with asyncio.timeout(SKIP_LOCKED_TIMEOUT_SECONDS):
            await PostgresJobHousekeeping(session_factory).purge_finished(at(DAY), BATCH_SIZE)
    finally:
        holder.release()
        await holding

    assert await store.get(held) is not None


async def test_sweep_fails_jobs_stuck_past_their_threshold_and_leaves_the_rest(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    crashed = await stored(store, cleanup, running().advance(JobStage.GENERATING_CARDS, 0.5, ANCIENT))
    lost = await stored(store, cleanup, queued())
    busy = running(timedelta(hours=1, minutes=50))
    waiting = queued(timedelta(minutes=90))
    await stored(store, cleanup, busy)
    await stored(store, cleanup, waiting)

    await sweeper(session_factory)()

    crashed_job = await store.get(crashed)
    lost_job = await store.get(lost)
    assert crashed_job is not None
    assert isinstance(crashed_job.state, Failed)
    assert (crashed_job.state.code, crashed_job.stage, crashed_job.updated_at) == (
        FailureCode.GENERATION_FAILED,
        JobStage.GENERATING_CARDS,
        SWEPT_AT,
    )
    assert lost_job is not None
    assert isinstance(lost_job.state, Failed)
    assert lost_job.state.code is FailureCode.GENERATION_FAILED
    assert await store.get(busy.job_id) == busy
    assert await store.get(waiting.job_id) == waiting


async def test_queued_job_with_an_invalidated_request_is_failed_when_the_worker_picks_it_up(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job_id = await stored(store, cleanup, GenerationJob.queue(uuid4(), utc_now()))
    await invalidate_saved_request(session_factory, job_id)
    providers = FakeProviders()

    await pipeline(store, providers)(job_id)

    job = await store.get(job_id)
    assert job is not None
    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.GENERATION_FAILED
    assert providers.calls == []


async def test_queued_job_with_an_invalidated_request_that_is_never_picked_up_is_swept(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job_id = await stored(store, cleanup, queued())
    await invalidate_saved_request(session_factory, job_id)

    await sweeper(session_factory)()

    job = await store.get(job_id)
    assert job is not None
    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.GENERATION_FAILED


async def test_sweep_waiting_on_a_worker_that_advances_the_job_leaves_it_running(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = running()
    await stored(store, cleanup, job)

    def advance(current: GenerationJob) -> GenerationJob:
        return current.advance(JobStage.RETRIEVING_SOURCES, 0.2, SWEPT_AT - timedelta(minutes=1))

    worker = RowHolder(settings, job.job_id)
    advancing = asyncio.create_task(worker.update(advance))
    await worker.until_locked()
    sweeping = asyncio.create_task(sweeper(session_factory)())
    try:
        await until_an_update_waits_on_the_row_lock(session_factory)
    finally:
        worker.release()
        advanced, swept = await asyncio.gather(advancing, sweeping, return_exceptions=True)

    assert isinstance(swept, int)
    assert advanced == advance(job)
    assert await store.get(job.job_id) == advance(job)


async def test_sweep_waiting_on_a_worker_that_claims_the_queued_job_leaves_it_running(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = queued()
    await stored(store, cleanup, job)

    def claim(current: GenerationJob) -> GenerationJob:
        return current.start(SWEPT_AT)

    worker = RowHolder(settings, job.job_id)
    claiming = asyncio.create_task(worker.update(claim))
    await worker.until_locked()
    sweeping = asyncio.create_task(sweeper(session_factory)())
    try:
        await until_an_update_waits_on_the_row_lock(session_factory)
    finally:
        worker.release()
        claimed, swept = await asyncio.gather(claiming, sweeping, return_exceptions=True)

    assert isinstance(swept, int)
    assert claimed == claim(job)
    assert await store.get(job.job_id) == claim(job)


async def test_worker_claiming_a_job_the_sweep_is_failing_waits_then_stops_without_calling_a_port(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = queued()
    await stored(store, cleanup, job)
    sweep = RowHolder(settings, job.job_id)
    failing = asyncio.create_task(
        sweep.update(fail_if_stale(JobStatus.QUEUED, SWEPT_AT - QUEUED_AFTER, SWEPT_AT))
    )
    await sweep.until_locked()
    providers = FakeProviders()
    working = asyncio.create_task(pipeline(store, providers)(job.job_id))
    try:
        await until_an_update_waits_on_the_row_lock(session_factory)
    finally:
        sweep.release()
        failed, worked = await asyncio.gather(failing, working, return_exceptions=True)

    assert failed == job.fail(FailureCode.GENERATION_FAILED, SWEPT_AT)
    assert worked is None
    assert providers.calls == []
    assert await store.get(job.job_id) == job.fail(FailureCode.GENERATION_FAILED, SWEPT_AT)


async def test_two_sweeps_racing_over_one_stuck_job_fail_it_exactly_once(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = running()
    await stored(store, cleanup, job)
    first, second = sweeper(session_factory), sweeper(session_factory, SWEPT_AT + timedelta(seconds=1))

    await asyncio.gather(first(), second())

    swept = await store.get(job.job_id)
    assert swept is not None
    assert isinstance(swept.state, Failed)
    assert swept.updated_at in {SWEPT_AT, SWEPT_AT + timedelta(seconds=1)}


async def release_key(settings: Settings, held: IdempotencyScope) -> None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        async with create_session_factory(engine).begin() as session:
            await session.execute(
                update(GenerationJobRow)
                .where(
                    GenerationJobRow.client_id == held.client_id,
                    GenerationJobRow.idempotency_key == held.idempotency_key,
                )
                .values(idempotency_key=None)
            )
    finally:
        await engine.dispose()


async def test_key_released_between_a_conflicting_insert_and_its_lookup_starts_a_new_job(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cleanup: Cleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresJobStore(session_factory)
    held = cleanup.scope()
    original = await store.add(queued(), generation_request(), held)
    real_scoped_row = job_store.scoped_row
    lookups: list[IdempotencyScope] = []

    def released_before_the_first_lookup(scope: IdempotencyScope) -> Select[tuple[GenerationJobRow]]:
        if not lookups:
            with ThreadPoolExecutor(max_workers=1) as elsewhere:
                elsewhere.submit(asyncio.run, release_key(settings, scope)).result()
        lookups.append(scope)
        return real_scoped_row(scope)

    monkeypatch.setattr(job_store, "scoped_row", released_before_the_first_lookup)
    replacement = queued(DAY)

    added = await store.add(replacement, generation_request(), held)

    assert added.job == replacement
    assert len(lookups) == 1
    assert await store.get(original.job.job_id) == original.job


async def test_retention_run_releases_expired_keys_and_purges_expired_jobs(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    held = cleanup.scope()
    waiting = await store.add(queued(), generation_request(), held)
    finished = await stored(store, cleanup, running().succeed(FULL_RESULT, ANCIENT))
    retention = EnforceJobRetention(
        housekeeping=PostgresJobHousekeeping(session_factory),
        policy=RetentionPolicy(idempotency_key_ttl=DAY, job_retention=DAY),
        batch_size=BATCH_SIZE,
        clock=lambda: at(DAY + timedelta(seconds=1)),
    )

    outcome = await retention()

    assert outcome.released_keys >= 1
    assert outcome.purged_jobs >= 1
    assert isinstance(outcome, RetentionOutcome)
    assert await store.find(held) is None
    assert await store.get(waiting.job.job_id) == waiting.job
    assert await store.get(finished) is None


async def test_one_unrestorable_stuck_job_does_not_stop_the_sweep_from_failing_the_others(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    poisoned = await stored(store, cleanup, running())
    stuck = await stored(store, cleanup, running(timedelta(seconds=1)))
    async with session_factory.begin() as session:
        await session.execute(
            update(GenerationJobRow).where(GenerationJobRow.job_id == poisoned).values(stage="summoning")
        )

    await sweeper(session_factory)()

    swept = await store.get(stuck)
    assert swept is not None
    assert isinstance(swept.state, Failed)
    async with session_factory() as session:
        row = await session.get(GenerationJobRow, poisoned)
    assert row is not None
    assert (row.status, row.stage) == ("running", "summoning")


async def test_one_retention_run_touches_at_most_one_batch_of_rows_oldest_first(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    older_scope, newer_scope = cleanup.scope(), cleanup.scope()
    older = await store.add(queued().cancel(ANCIENT), generation_request(), older_scope)
    newer = await store.add(
        queued(timedelta(seconds=1)).cancel(at(timedelta(seconds=1))), generation_request(), newer_scope
    )
    housekeeping = PostgresJobHousekeeping(session_factory)

    released = await housekeeping.release_idempotency_keys(at(DAY), 1)
    purged = await housekeeping.purge_finished(at(DAY), 1)

    assert (released, purged) == (1, 1)
    assert await store.find(newer_scope) == newer
    assert await store.get(older.job.job_id) is None
    assert await store.get(newer.job.job_id) == newer.job
