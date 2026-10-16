import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import IdempotencyScope
from deckly.config import Settings
from deckly.domain.job import GenerationJob
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.tables import GenerationJobRow
from tests.fakes import generation_request
from tests.integration.conftest import DOCUMENT_RETENTION, Cleanup
from tests.reserved_clients import ABANDONED_AFTER, purge_abandoned_test_jobs, reserved_client_id

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2000, 1, 1, 12, tzinfo=UTC)
LONG_AGO = datetime(1999, 1, 1, tzinfo=UTC)


async def leftover(
    session_factory: async_sessionmaker[AsyncSession],
    cleanup: Cleanup,
    client_id: UUID,
    created_at: datetime = LONG_AGO,
) -> UUID:
    cleanup.client_ids.add(client_id)
    job = GenerationJob.queue(uuid4(), created_at)
    scope = IdempotencyScope(client_id=client_id, idempotency_key=uuid4())
    await PostgresJobStore(session_factory, document_retention=DOCUMENT_RETENTION).add(
        job, generation_request(), scope
    )
    return job.job_id


async def surviving(session_factory: async_sessionmaker[AsyncSession], job_ids: set[UUID]) -> set[UUID]:
    async with session_factory() as session:
        found = await session.scalars(
            select(GenerationJobRow.job_id).where(GenerationJobRow.job_id.in_(job_ids))
        )
        return set(found)


async def test_a_row_left_behind_by_a_killed_run_is_purged(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    abandoned = await leftover(session_factory, cleanup, reserved_client_id(NOW - 2 * ABANDONED_AFTER))

    purged = await purge_abandoned_test_jobs(settings, NOW)

    assert purged == 1
    assert await surviving(session_factory, {abandoned}) == set()


async def test_the_age_comes_from_the_client_id_not_from_the_rows_own_timestamps(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    running_now = await leftover(
        session_factory, cleanup, reserved_client_id(NOW - timedelta(minutes=5)), created_at=LONG_AGO
    )
    minted_ahead = await leftover(session_factory, cleanup, reserved_client_id(NOW + timedelta(days=1)))

    purged = await purge_abandoned_test_jobs(settings, NOW)

    assert purged == 0
    assert await surviving(session_factory, {running_now, minted_ahead}) == {running_now, minted_ahead}


async def test_a_row_is_purged_only_once_strictly_older_than_the_threshold(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    on_the_line = await leftover(session_factory, cleanup, reserved_client_id(NOW - ABANDONED_AFTER))
    just_past = await leftover(
        session_factory, cleanup, reserved_client_id(NOW - ABANDONED_AFTER - timedelta(seconds=1))
    )

    await purge_abandoned_test_jobs(settings, NOW)

    assert await surviving(session_factory, {on_the_line, just_past}) == {on_the_line}


@pytest.mark.parametrize(
    "client_id",
    [
        UUID(int=0, version=4),
        UUID("00000000-0000-4000-8000-000000000000"),
        UUID("00000000-7e57-47e5-8de2-0123456789ab"),
        UUID("00000000-7e57-47e6-8de1-0123456789ab"),
        UUID("00000000-7e58-47e5-8de1-0123456789ab"),
        UUID("00000000-7e57-4000-8de1-0123456789ab"),
    ],
    ids=["nil", "zero-time", "variant-tail", "version-tail", "namespace-head", "namespace-body"],
)
async def test_rows_outside_the_reserved_namespace_are_never_touched(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cleanup: Cleanup,
    client_id: UUID,
) -> None:
    real_traffic = await leftover(session_factory, cleanup, client_id)

    await purge_abandoned_test_jobs(settings, NOW)

    assert await surviving(session_factory, {real_traffic}) == {real_traffic}


async def test_an_ordinary_client_row_is_never_touched_however_old(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    ordinary = await leftover(session_factory, cleanup, uuid4(), created_at=datetime(1971, 1, 1, tzinfo=UTC))

    await purge_abandoned_test_jobs(settings, datetime(2100, 1, 1, tzinfo=UTC))

    assert await surviving(session_factory, {ordinary}) == {ordinary}


async def test_every_row_a_reserved_client_leaves_is_purged_together(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    client_id = reserved_client_id(NOW - 3 * ABANDONED_AFTER)
    first = await leftover(session_factory, cleanup, client_id)
    second = await leftover(session_factory, cleanup, client_id)

    purged = await purge_abandoned_test_jobs(settings, NOW)

    assert purged == 2
    assert await surviving(session_factory, {first, second}) == set()


async def test_two_sessions_starting_together_purge_the_same_rows_without_error(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    abandoned = {
        await leftover(session_factory, cleanup, reserved_client_id(NOW - 2 * ABANDONED_AFTER))
        for _ in range(50)
    }

    purged = await asyncio.gather(
        purge_abandoned_test_jobs(settings, NOW), purge_abandoned_test_jobs(settings, NOW)
    )

    assert sum(purged) == len(abandoned)
    assert await surviving(session_factory, abandoned) == set()


def test_the_purge_runs_at_the_start_of_every_integration_session(request: pytest.FixtureRequest) -> None:
    assert "abandoned_test_jobs_purged" in request.fixturenames
