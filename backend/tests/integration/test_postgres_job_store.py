import asyncio
from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import IdempotencyScope, JobTransition
from deckly.config import Settings
from deckly.domain.exceptions import JobAlreadyTerminalError
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import FailureCode, GenerationJob, JobStage, JobStatus
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.job_store import PostgresJobStore, UnstorableJobStateError
from deckly.infrastructure.tables import GenerationJobRow
from tests.domain.builders import T0, at, basic_note, result_with
from tests.fakes import generation_request
from tests.integration.conftest import Cleanup

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONCURRENT_REPLAYS = 20
CONCURRENT_CANCELS = 20


def new_job() -> GenerationJob:
    return GenerationJob.queue(uuid4(), T0)


async def rows_for(
    session_factory: async_sessionmaker[AsyncSession], job: GenerationJob
) -> list[GenerationJobRow]:
    async with session_factory() as session:
        rows = await session.scalars(select(GenerationJobRow).where(GenerationJobRow.job_id == job.job_id))
        return list(rows)


async def count_for_client(session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).where(GenerationJobRow.client_id.in_(cleanup.client_ids))
        )
        return count or 0


async def test_new_job_is_persisted_with_its_request(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = new_job()
    request = replace(
        generation_request(),
        difficulty=Difficulty.ADVANCED,
        note_types=(NoteType.CLOZE, NoteType.BASIC),
        include_images=True,
        instructions="Focus on warning signs",
    )

    stored = await store.add(job, request, cleanup.scope())

    assert stored.job == job
    assert stored.request == request
    [row] = await rows_for(session_factory, job)
    assert (row.status, row.stage, row.progress) == ("queued", None, 0.0)
    assert (row.topic, row.language, row.card_count) == (request.topic, request.language, request.card_count)
    assert row.difficulty == "advanced"
    assert row.note_types == ["cloze", "basic"]
    assert row.include_images is True
    assert row.instructions == "Focus on warning signs"
    assert row.created_at == T0


ROUND_TRIPPED_REQUESTS = {
    "every field set": replace(
        generation_request(),
        language="en-GB",
        card_count=77,
        difficulty=Difficulty.ADVANCED,
        note_types=(NoteType.CLOZE, NoteType.BASIC),
        include_images=True,
        instructions="Focus on warning signs",
    ),
    "empty instructions": replace(generation_request(), instructions=""),
    "decomposed and astral text": replace(
        generation_request("Cafe\N{COMBINING ACUTE ACCENT} \N{GRINNING FACE}"), language="zh-Hant-TW"
    ),
}


@pytest.mark.parametrize(
    "original_request", ROUND_TRIPPED_REQUESTS.values(), ids=ROUND_TRIPPED_REQUESTS.keys()
)
async def test_replaying_a_scope_returns_the_original_job_and_its_original_request(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup, original_request: GenerationRequest
) -> None:
    store = PostgresJobStore(session_factory)
    scope = cleanup.scope()
    original = await store.add(new_job(), original_request, scope)

    replay = await store.add(
        GenerationJob.queue(uuid4(), at(60)), generation_request("A different topic"), scope
    )

    assert replay == original
    assert replay.request == original_request
    [row] = await rows_for(session_factory, original.job)
    assert row.topic == original_request.topic
    assert await count_for_client(session_factory, cleanup) == 1


async def test_same_key_from_another_client_is_a_separate_job(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    first_scope = cleanup.scope()
    other_scope = replace(cleanup.scope(), idempotency_key=first_scope.idempotency_key)

    first = await store.add(new_job(), generation_request(), first_scope)
    other = await store.add(new_job(), generation_request(), other_scope)

    assert first.job.job_id != other.job.job_id
    assert await count_for_client(session_factory, cleanup) == 2


async def test_concurrent_requests_with_one_scope_create_exactly_one_job(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    scope = cleanup.scope()

    results = await asyncio.gather(
        *(store.add(new_job(), generation_request(), scope) for _ in range(CONCURRENT_REPLAYS))
    )

    assert len({stored.job.job_id for stored in results}) == 1
    assert await count_for_client(session_factory, cleanup) == 1


async def add_through_a_fresh_pool(
    settings: Settings, job: GenerationJob, scope: IdempotencyScope
) -> GenerationJob:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        stored = await PostgresJobStore(create_session_factory(engine)).add(job, generation_request(), scope)
        return stored.job
    finally:
        await engine.dispose()


async def test_a_stored_job_survives_a_new_connection_pool(settings: Settings, cleanup: Cleanup) -> None:
    scope = cleanup.scope()
    original = await add_through_a_fresh_pool(settings, new_job(), scope)

    restored = await add_through_a_fresh_pool(settings, new_job(), scope)

    assert restored == original
    assert restored.status is JobStatus.QUEUED
    assert restored.created_at == T0


async def test_succeeded_job_is_refused_and_nothing_is_written(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    succeeded = new_job().start(at(1)).succeed(result_with(basic_note(1)), at(2))

    with pytest.raises(UnstorableJobStateError):
        await PostgresJobStore(session_factory).add(succeeded, generation_request(), cleanup.scope())

    assert await rows_for(session_factory, succeeded) == []


async def test_replays_from_two_clients_sharing_a_key_each_get_their_own_job(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    first_scope = cleanup.scope()
    other_scope = replace(cleanup.scope(), idempotency_key=first_scope.idempotency_key)
    first = await store.add(new_job(), generation_request(), first_scope)
    other = await store.add(new_job(), generation_request(), other_scope)

    first_replay = await store.add(new_job(), generation_request(), first_scope)
    other_replay = await store.add(new_job(), generation_request(), other_scope)

    assert (first_replay.job.job_id, other_replay.job.job_id) == (first.job.job_id, other.job.job_id)


STORED_STATES: dict[str, Callable[[GenerationJob], GenerationJob]] = {
    "queued": lambda job: job,
    "running": lambda job: job.start(at(1)).advance(JobStage.GENERATING_CARDS, 0.62, at(44)),
    "failed": lambda job: job.start(at(1)).fail(FailureCode.PROVIDER_UNAVAILABLE, at(2)),
    "cancelled": lambda job: job.cancel(at(3)),
}


@pytest.mark.parametrize("transition", STORED_STATES.values(), ids=STORED_STATES.keys())
async def test_get_returns_the_job_exactly_as_it_was_stored(
    session_factory: async_sessionmaker[AsyncSession],
    cleanup: Cleanup,
    transition: Callable[[GenerationJob], GenerationJob],
) -> None:
    store = PostgresJobStore(session_factory)
    job = transition(new_job())
    await store.add(job, generation_request(), cleanup.scope())

    assert await store.get(job.job_id) == job


async def test_get_of_an_unknown_job_is_none(session_factory: async_sessionmaker[AsyncSession]) -> None:
    assert await PostgresJobStore(session_factory).get(uuid4()) is None


ACTIVE_STATES = {name: STORED_STATES[name] for name in ("queued", "running")}
TERMINAL_STATES = {name: STORED_STATES[name] for name in ("failed", "cancelled")}


@pytest.mark.parametrize("transition", ACTIVE_STATES.values(), ids=ACTIVE_STATES.keys())
async def test_update_persists_a_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
    cleanup: Cleanup,
    transition: Callable[[GenerationJob], GenerationJob],
) -> None:
    store = PostgresJobStore(session_factory)
    job = transition(new_job())
    await store.add(job, generation_request(), cleanup.scope())

    cancelled = await store.update(job.job_id, lambda stored: stored.cancel(at(100)))

    assert cancelled == job.cancel(at(100))
    assert await store.get(job.job_id) == cancelled
    [row] = await rows_for(session_factory, job)
    assert (row.status, row.stage, row.progress) == ("cancelled", job.stage, job.progress.value)
    assert (row.failure_reason, row.updated_at, row.created_at) == (None, at(100), T0)


@pytest.mark.parametrize("transition", TERMINAL_STATES.values(), ids=TERMINAL_STATES.keys())
async def test_update_that_raises_leaves_the_row_unchanged(
    session_factory: async_sessionmaker[AsyncSession],
    cleanup: Cleanup,
    transition: Callable[[GenerationJob], GenerationJob],
) -> None:
    store = PostgresJobStore(session_factory)
    job = transition(new_job())
    await store.add(job, generation_request(), cleanup.scope())

    with pytest.raises(JobAlreadyTerminalError):
        await store.update(job.job_id, lambda stored: stored.cancel(at(100)))

    assert await store.get(job.job_id) == job


async def test_update_of_an_unknown_job_is_none(session_factory: async_sessionmaker[AsyncSession]) -> None:
    def unreachable(job: GenerationJob) -> GenerationJob:
        raise AssertionError(job)

    assert await PostgresJobStore(session_factory).update(uuid4(), unreachable) is None


def cancel_at(second: int) -> JobTransition:
    return lambda stored: stored.cancel(at(second))


async def test_concurrent_cancels_of_one_job_succeed_exactly_once(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = new_job().start(at(1))
    await store.add(job, generation_request(), cleanup.scope())

    outcomes = await asyncio.gather(
        *(store.update(job.job_id, cancel_at(second)) for second in range(100, 100 + CONCURRENT_CANCELS)),
        return_exceptions=True,
    )

    winners = [outcome for outcome in outcomes if isinstance(outcome, GenerationJob)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, JobAlreadyTerminalError)]
    assert (len(winners), len(losers)) == (1, CONCURRENT_CANCELS - 1)
    assert await store.get(job.job_id) == winners[0]
