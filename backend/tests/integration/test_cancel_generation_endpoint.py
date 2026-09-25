import asyncio
import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from deckly.config import Settings
from deckly.domain.job import FailureCode, GenerationJob, JobStage
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.tables import GenerationJobRow
from deckly.main import API_PREFIX, create_app
from tests.domain.builders import T0, at
from tests.fakes import generation_request
from tests.integration.conftest import Cleanup
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.integration

ENDPOINT = f"{API_PREFIX}/generations"
PAYLOAD = {"topic": "Road signs", "language": "ru", "cardCount": 40}


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
        cleanup.job_ids.add(job.job_id)
        await PostgresJobStore(create_session_factory(engine)).add(job, generation_request(), cleanup.scope())
    finally:
        await engine.dispose()


def headers() -> dict[str, str]:
    return {"X-Client-Id": str(uuid4())}


def cancel(client: TestClient, job_id: object) -> tuple[int, bytes, dict[str, object] | None]:
    response = client.post(f"{ENDPOINT}/{job_id}/cancel", headers=headers())
    if response.status_code == HTTPStatus.NO_CONTENT:
        return response.status_code, response.content, None
    body: dict[str, object] = response.json()
    assert spec_errors("Problem", body) == []
    return response.status_code, response.content, body


def poll(client: TestClient, job_id: object) -> dict[str, object]:
    response = client.get(f"{ENDPOINT}/{job_id}", headers=headers())
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, object] = response.json()
    assert spec_errors("GenerationJob", body) == []
    return body


def queued() -> GenerationJob:
    return GenerationJob.queue(uuid4(), T0)


def failed() -> GenerationJob:
    return queued().start(at(1)).fail(FailureCode.PROVIDER_UNAVAILABLE, at(2))


ACTIVE_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "queued": queued,
    "running": lambda: queued().start(at(1)).advance(JobStage.PARSING_SOURCES, 0.3, at(20)),
}
TERMINAL_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "failed": failed,
    "cancelled": lambda: queued().start(at(1)).cancel(at(2)),
}


@pytest.mark.parametrize("make_job", ACTIVE_JOBS.values(), ids=ACTIVE_JOBS.keys())
def test_active_job_is_cancelled_and_polls_as_cancelled(
    client: TestClient, cleanup: Cleanup, settings: Settings, make_job: Callable[[], GenerationJob]
) -> None:
    job = make_job()
    asyncio.run(write(settings, cleanup, job))

    assert cancel(client, job.job_id) == (HTTPStatus.NO_CONTENT, b"", None)

    body = poll(client, job.job_id)
    assert (body["status"], body["stage"], body["progress"]) == ("cancelled", job.stage, job.progress.value)
    assert isinstance(body["updatedAt"], str)
    assert datetime.fromisoformat(body["updatedAt"]) > job.updated_at


@pytest.mark.parametrize("make_job", TERMINAL_JOBS.values(), ids=TERMINAL_JOBS.keys())
def test_terminal_job_is_409_and_unchanged(
    client: TestClient, cleanup: Cleanup, settings: Settings, make_job: Callable[[], GenerationJob]
) -> None:
    job = make_job()
    asyncio.run(write(settings, cleanup, job))
    before = poll(client, job.job_id)

    status, _, body = cancel(client, job.job_id)

    assert status == HTTPStatus.CONFLICT
    assert body is not None
    assert body["code"] == "JOB_ALREADY_TERMINAL"
    assert poll(client, job.job_id) == before


def test_job_created_through_the_api_can_be_cancelled_once(client: TestClient, cleanup: Cleanup) -> None:
    client_id = uuid4()
    cleanup.client_ids.add(client_id)
    created = client.post(
        ENDPOINT, json=PAYLOAD, headers={"Idempotency-Key": str(uuid4()), "X-Client-Id": str(client_id)}
    ).json()
    cleanup.job_ids.add(UUID(created["jobId"]))

    first_status, _, _ = cancel(client, created["jobId"])
    second_status, _, second_body = cancel(client, created["jobId"])

    assert (first_status, second_status) == (HTTPStatus.NO_CONTENT, HTTPStatus.CONFLICT)
    assert second_body is not None
    assert second_body["code"] == "JOB_ALREADY_TERMINAL"
    assert poll(client, created["jobId"])["status"] == "cancelled"


def test_unknown_job_is_job_not_found(client: TestClient) -> None:
    status, _, body = cancel(client, uuid4())

    assert status == HTTPStatus.NOT_FOUND
    assert body is not None
    assert body["code"] == "JOB_NOT_FOUND"


async def mark_succeeded_without_a_result(settings: Settings, job_id: UUID) -> None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        async with create_session_factory(engine).begin() as session:
            await session.execute(
                update(GenerationJobRow)
                .where(GenerationJobRow.job_id == job_id)
                .values(status="succeeded", stage=None, progress=1.0)
            )
    finally:
        await engine.dispose()


async def stored_status(settings: Settings, job_id: UUID) -> str | None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        async with create_session_factory(engine)() as session:
            row = await session.get(GenerationJobRow, job_id)
            return None if row is None else row.status
    finally:
        await engine.dispose()


def test_unrestorable_row_is_internal_error_and_left_untouched(
    client: TestClient, cleanup: Cleanup, settings: Settings
) -> None:
    job = queued()
    asyncio.run(write(settings, cleanup, job))
    asyncio.run(mark_succeeded_without_a_result(settings, job.job_id))

    status, _, body = cancel(client, job.job_id)

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body is not None
    assert body["code"] == "INTERNAL_ERROR"
    assert "detail" not in body
    assert asyncio.run(stored_status(settings, job.job_id)) == "succeeded"
