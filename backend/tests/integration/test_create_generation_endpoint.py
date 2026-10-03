import asyncio
import logging
import statistics
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from arq.jobs import Job
from fastapi.testclient import TestClient
from sqlalchemy import update

from deckly.application.housekeeping import EnforceJobRetention, RetentionPolicy
from deckly.config import Settings
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.job_store import PostgresJobHousekeeping
from deckly.infrastructure.tables import GenerationJobRow
from deckly.main import API_PREFIX, create_app
from tests.integration.conftest import Cleanup, open_queue_pool, with_generation_limits
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.integration

ENDPOINT = f"{API_PREFIX}/generations"
PAYLOAD = {"topic": "Road signs", "language": "ru", "cardCount": 40}
WARMUP_REQUESTS = 10
MEASURED_REQUESTS = 200
LATENCY_BUDGET_SECONDS = 0.5
P95_INDEX = 18
JOB_FIELDS = ("jobId", "status", "createdAt")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        with TestClient(create_app(settings)) as test_client:
            yield test_client
    finally:
        root.handlers, root.level = handlers, level


def post(client: TestClient, cleanup: Cleanup, client_id: UUID, key: UUID) -> dict[str, object]:
    cleanup.client_ids.add(client_id)
    response = client.post(
        ENDPOINT, json=PAYLOAD, headers={"Idempotency-Key": str(key), "X-Client-Id": str(client_id)}
    )
    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    body: dict[str, object] = response.json()
    assert spec_errors("GenerationJobCreated", body) == []
    cleanup.job_ids.add(UUID(str(body["jobId"])))
    return body


async def queued_job_exists(settings: Settings, job_id: str) -> bool:
    pool = await open_queue_pool(settings)
    try:
        return await Job(job_id, pool).info() is not None
    finally:
        await pool.aclose()


def test_created_job_is_persisted_enqueued_and_replayable(
    client: TestClient, cleanup: Cleanup, settings: Settings
) -> None:
    client_id, key = uuid4(), uuid4()

    first = post(client, cleanup, client_id, key)
    replay = post(client, cleanup, client_id, key)
    other = post(client, cleanup, uuid4(), key)

    assert first["status"] == "queued"
    assert {key: replay[key] for key in JOB_FIELDS} == {key: first[key] for key in JOB_FIELDS}
    assert other["jobId"] != first["jobId"]
    assert asyncio.run(queued_job_exists(settings, str(first["jobId"])))


def test_reusing_a_key_for_a_different_body_is_a_conflict(client: TestClient, cleanup: Cleanup) -> None:
    client_id, key = uuid4(), uuid4()
    first = post(client, cleanup, client_id, key)

    response = client.post(
        ENDPOINT,
        json={**PAYLOAD, "topic": "A different topic"},
        headers={"Idempotency-Key": str(key), "X-Client-Id": str(client_id)},
    )
    replay = post(client, cleanup, client_id, key)

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert spec_errors("Problem", response.json()) == []
    assert replay["jobId"] == first["jobId"]


@pytest.fixture
def unmetered_client(settings: Settings) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    total = WARMUP_REQUESTS + MEASURED_REQUESTS
    try:
        with TestClient(
            create_app(with_generation_limits(settings, per_client=total, per_address=total))
        ) as test_client:
            yield test_client
    finally:
        root.handlers, root.level = handlers, level


def test_p95_latency_is_within_budget(unmetered_client: TestClient, cleanup: Cleanup) -> None:
    client = unmetered_client
    client_id = uuid4()
    for _ in range(WARMUP_REQUESTS):
        post(client, cleanup, client_id, uuid4())

    durations = []
    for _ in range(MEASURED_REQUESTS):
        started = time.perf_counter()
        post(client, cleanup, client_id, uuid4())
        durations.append(time.perf_counter() - started)

    assert statistics.quantiles(durations, n=20)[P95_INDEX] < LATENCY_BUDGET_SECONDS


@pytest.mark.parametrize("field", ["topic", "instructions"])
def test_text_postgres_cannot_store_is_validation_failed_not_internal_error(
    client: TestClient, field: str
) -> None:
    response = client.post(
        ENDPOINT,
        json={**PAYLOAD, field: "Road\x00signs"},
        headers={"Idempotency-Key": str(uuid4()), "X-Client-Id": str(uuid4())},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["code"] == "VALIDATION_FAILED"


AGED_AT = datetime(2000, 1, 1, tzinfo=UTC)


async def age_and_enforce_retention(settings: Settings, job_id: UUID) -> None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory.begin() as session:
            await session.execute(
                update(GenerationJobRow)
                .where(GenerationJobRow.job_id == job_id)
                .values(created_at=AGED_AT, updated_at=AGED_AT)
            )
        ttl = timedelta(seconds=settings.cache.idempotency_key_ttl_seconds)
        retention = EnforceJobRetention(
            housekeeping=PostgresJobHousekeeping(session_factory),
            policy=RetentionPolicy(
                idempotency_key_ttl=ttl, job_retention=timedelta(seconds=settings.cache.job_retention_seconds)
            ),
            batch_size=settings.sweep.batch_size,
            clock=lambda: AGED_AT + ttl + timedelta(seconds=1),
        )
        await retention()
    finally:
        await engine.dispose()


def test_key_older_than_its_ttl_starts_a_new_job_and_the_original_stays_pollable(
    client: TestClient, cleanup: Cleanup, settings: Settings
) -> None:
    client_id, key = uuid4(), uuid4()
    original = post(client, cleanup, client_id, key)
    asyncio.run(age_and_enforce_retention(settings, UUID(str(original["jobId"]))))

    renewed = post(client, cleanup, client_id, key)
    replay = post(client, cleanup, client_id, key)
    polled = client.get(f"{ENDPOINT}/{original['jobId']}", headers={"X-Client-Id": str(client_id)})

    assert renewed["jobId"] != original["jobId"]
    assert replay["jobId"] == renewed["jobId"]
    assert polled.status_code == HTTPStatus.OK
    assert polled.json()["jobId"] == original["jobId"]
