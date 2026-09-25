import asyncio
import logging
import statistics
import time
from collections.abc import Callable, Iterator
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from deckly.config import Settings
from deckly.domain.job import Failed, FailureCode, GenerationJob, JobStage
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.job_store import PostgresJobStore, store_state
from deckly.infrastructure.tables import GenerationJobRow
from deckly.main import API_PREFIX, create_app
from deckly.transport.generations import POLL_RETRY_AFTER_SECONDS
from tests.domain.builders import T0, at
from tests.fakes import generation_request
from tests.integration.conftest import Cleanup
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.integration

ENDPOINT = f"{API_PREFIX}/generations"
PAYLOAD = {"topic": "Road signs", "language": "ru", "cardCount": 40}
WARMUP_REQUESTS = 10
MEASURED_REQUESTS = 200
LATENCY_BUDGET_SECONDS = 0.2
P95_INDEX = 18
RETRY_AFTER = "retry-after"


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        root.handlers, root.level = handlers, level


async def write(settings: Settings, cleanup: Cleanup, job: GenerationJob) -> None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        session_factory = create_session_factory(engine)
        if job.job_id not in cleanup.job_ids:
            cleanup.job_ids.add(job.job_id)
            await PostgresJobStore(session_factory).add(job, generation_request(), cleanup.scope())
            return
        stored = store_state(job.state)
        async with session_factory.begin() as session:
            await session.execute(
                update(GenerationJobRow)
                .where(GenerationJobRow.job_id == job.job_id)
                .values(
                    status=stored.status,
                    stage=stored.stage,
                    progress=stored.progress,
                    failure_reason=stored.failure_reason,
                    updated_at=job.updated_at,
                )
            )
    finally:
        await engine.dispose()


def poll(client: TestClient, job_id: object) -> dict[str, object]:
    response = client.get(f"{ENDPOINT}/{job_id}", headers={"X-Client-Id": str(uuid4())})
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, object] = response.json()
    assert spec_errors("GenerationJob", body) == []
    assert (RETRY_AFTER in response.headers) == (body["status"] in {"queued", "running"})
    return body


def queued() -> GenerationJob:
    return GenerationJob.queue(uuid4(), T0)


def failed_with(code: FailureCode) -> Callable[[], GenerationJob]:
    return lambda: queued().start(at(1)).fail(code, at(2))


STORED_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "running": lambda: queued().start(at(1)).advance(JobStage.PARSING_SOURCES, 0.3, at(20)),
    **{f"failed with {code}": failed_with(code) for code in FailureCode},
    "cancelled": lambda: queued().start(at(1)).cancel(at(2)),
}


def test_job_created_through_the_api_polls_as_queued(client: TestClient, cleanup: Cleanup) -> None:
    client_id = uuid4()
    cleanup.client_ids.add(client_id)
    created = client.post(
        ENDPOINT, json=PAYLOAD, headers={"Idempotency-Key": str(uuid4()), "X-Client-Id": str(client_id)}
    ).json()
    cleanup.job_ids.add(UUID(created["jobId"]))

    body = poll(client, created["jobId"])

    assert (body["jobId"], body["status"], body["createdAt"]) == (
        created["jobId"],
        "queued",
        created["createdAt"],
    )


@pytest.mark.parametrize("make_job", STORED_JOBS.values(), ids=STORED_JOBS.keys())
def test_every_storable_state_polls_as_200_against_the_spec(
    client: TestClient, cleanup: Cleanup, settings: Settings, make_job: Callable[[], GenerationJob]
) -> None:
    job = make_job()
    asyncio.run(write(settings, cleanup, job))

    body = poll(client, job.job_id)

    assert (body["status"], body["stage"], body["progress"]) == (job.status, job.stage, job.progress.value)
    assert body["updatedAt"] == job.updated_at.isoformat().replace("+00:00", "Z")
    error = body["error"]
    if isinstance(job.state, Failed):
        assert isinstance(error, dict)
        assert error["code"] == job.state.code
    else:
        assert error is None


def test_progress_never_decreases_while_the_stored_job_advances(
    client: TestClient, cleanup: Cleanup, settings: Settings
) -> None:
    job = queued()
    asyncio.run(write(settings, cleanup, job))
    job = job.start(at(1))
    progress: list[float] = []

    for second, stage in enumerate(JobStage, start=2):
        for fraction in (0.0, 0.05):
            job = job.advance(stage, round(0.1 * stage.position + fraction, 2), at(second))
            asyncio.run(write(settings, cleanup, job))
            body = poll(client, job.job_id)
            assert body["stage"] == stage
            assert isinstance(body["progress"], float)
            progress.append(body["progress"])

    assert progress == sorted(progress)
    assert len(progress) == 2 * len(JobStage)


def test_unknown_job_is_job_not_found(client: TestClient) -> None:
    response = client.get(f"{ENDPOINT}/{uuid4()}", headers={"X-Client-Id": str(uuid4())})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "JOB_NOT_FOUND"
    assert spec_errors("Problem", response.json()) == []


def test_retry_after_matches_the_client_poll_interval(
    client: TestClient, cleanup: Cleanup, settings: Settings
) -> None:
    job = queued()
    asyncio.run(write(settings, cleanup, job))

    response = client.get(f"{ENDPOINT}/{job.job_id}", headers={"X-Client-Id": str(uuid4())})

    assert response.headers[RETRY_AFTER] == str(POLL_RETRY_AFTER_SECONDS)


UNRESTORABLE_ROWS: dict[str, dict[str, object]] = {
    "succeeded without a stored result": {"status": "succeeded", "stage": None, "progress": 1.0},
    "progress outside the unit interval": {"status": "running", "stage": "planning", "progress": 1.5},
}


async def corrupt(settings: Settings, job_id: UUID, values: dict[str, object]) -> None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        async with create_session_factory(engine).begin() as session:
            await session.execute(
                update(GenerationJobRow).where(GenerationJobRow.job_id == job_id).values(**values)
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("values", UNRESTORABLE_ROWS.values(), ids=UNRESTORABLE_ROWS.keys())
def test_stored_row_that_cannot_be_restored_is_internal_error_without_leaking_it(
    client: TestClient, cleanup: Cleanup, settings: Settings, values: dict[str, object]
) -> None:
    job = queued()
    asyncio.run(write(settings, cleanup, job))
    asyncio.run(corrupt(settings, job.job_id, values))

    response = client.get(f"{ENDPOINT}/{job.job_id}", headers={"X-Client-Id": str(uuid4())})

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert spec_errors("Problem", response.json()) == []
    assert "detail" not in response.json()


def test_p95_latency_is_within_budget(client: TestClient, cleanup: Cleanup, settings: Settings) -> None:
    job = queued().start(at(1)).advance(JobStage.GENERATING_CARDS, 0.62, at(44))
    asyncio.run(write(settings, cleanup, job))
    for _ in range(WARMUP_REQUESTS):
        poll(client, job.job_id)

    durations = []
    for _ in range(MEASURED_REQUESTS):
        started = time.perf_counter()
        poll(client, job.job_id)
        durations.append(time.perf_counter() - started)

    assert statistics.quantiles(durations, n=20)[P95_INDEX] < LATENCY_BUDGET_SECONDS
