import asyncio
import logging
from collections.abc import Iterator
from dataclasses import replace
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.generations import CancelGeneration
from deckly.application.pipeline import RunGeneration
from deckly.application.ports import JobStore
from deckly.config import Settings
from deckly.domain.job import Cancelled, Failed, FailureCode, GenerationJob, JobStage
from deckly.domain.media import MediaKind
from deckly.domain.notes.basic import BasicFields
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.main import API_PREFIX, create_app
from tests.domain.builders import FULL_RESULT, T0, basic_note, result_with
from tests.fakes import FakeProviders, generation_request
from tests.integration.conftest import Cleanup
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.integration

ENDPOINT = f"{API_PREFIX}/generations"
PAYLOAD = {"topic": "Road signs", "language": "ru", "cardCount": 40, "includeImages": True}


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        root.handlers, root.level = handlers, level


def pipeline(store: JobStore, providers: FakeProviders) -> RunGeneration:
    return RunGeneration(
        store=store,
        retriever=providers,
        parser=providers,
        generator=providers,
        media=providers,
        clock=utc_now,
    )


async def run_in_its_own_pool(settings: Settings, providers: FakeProviders, job_id: UUID) -> None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        await pipeline(PostgresJobStore(create_session_factory(engine)), providers)(job_id)
    finally:
        await engine.dispose()


async def queued_in(store: PostgresJobStore, cleanup: Cleanup) -> GenerationJob:
    job = GenerationJob.queue(uuid4(), T0)
    await store.add(job, generation_request(), cleanup.scope())
    return job


def test_generation_created_through_the_api_runs_and_polls_back_as_succeeded(
    client: TestClient, cleanup: Cleanup, settings: Settings
) -> None:
    client_id = uuid4()
    cleanup.client_ids.add(client_id)
    created = client.post(
        ENDPOINT, json=PAYLOAD, headers={"Idempotency-Key": str(uuid4()), "X-Client-Id": str(client_id)}
    ).json()
    job_id = UUID(created["jobId"])
    cleanup.job_ids.add(job_id)
    providers = FakeProviders()
    providers.result = FULL_RESULT

    asyncio.run(run_in_its_own_pool(settings, providers, job_id))

    response = client.get(f"{ENDPOINT}/{job_id}", headers={"X-Client-Id": str(uuid4())})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert spec_errors("GenerationJob", body) == []
    assert (body["status"], body["stage"], body["progress"], body["error"]) == ("succeeded", None, 1.0, None)
    notes = body["result"]["notes"]
    assert [note["clientId"] for note in notes] == [str(note.client_id) for note in FULL_RESULT.notes]
    assert all(any(item["kind"] == MediaKind.IMAGE for item in note["media"]) for note in notes)


@pytest.mark.anyio
async def test_result_postgres_cannot_store_fails_the_job_instead_of_leaving_it_running(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = await queued_in(store, cleanup)
    providers = FakeProviders()
    providers.result = result_with(
        replace(basic_note(1), fields=BasicFields(front="Red\x00triangle", back="b"))
    )

    await pipeline(store, providers)(job.job_id)

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Failed)
    assert (stored.state.code, stored.stage) == (FailureCode.GENERATION_FAILED, JobStage.FINALIZING)


@pytest.mark.anyio
async def test_cancel_during_generation_stops_the_pipeline_through_postgres(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = await queued_in(store, cleanup)
    providers = FakeProviders()
    cancel = CancelGeneration(store=store, clock=utc_now)
    providers.during[JobStage.GENERATING_CARDS] = lambda: cancel(job.job_id)

    await pipeline(store, providers)(job.job_id)

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Cancelled)
    assert stored.stage is JobStage.GENERATING_CARDS
    assert providers.calls == [
        JobStage.RETRIEVING_SOURCES,
        JobStage.PARSING_SOURCES,
        JobStage.GENERATING_CARDS,
    ]


@pytest.mark.anyio
async def test_worker_interruption_is_recorded_as_generation_failed_in_postgres(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = await queued_in(store, cleanup)
    providers = FakeProviders()
    reached = asyncio.Event()

    async def hang() -> None:
        reached.set()
        await asyncio.Event().wait()

    providers.during[JobStage.GENERATING_CARDS] = hang
    running = asyncio.create_task(pipeline(store, providers)(job.job_id))
    await reached.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Failed)
    assert (stored.state.code, stored.stage) == (FailureCode.GENERATION_FAILED, JobStage.GENERATING_CARDS)
