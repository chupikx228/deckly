import asyncio
import logging
import statistics
import time
from collections.abc import Iterator
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from arq.jobs import Job
from fastapi.testclient import TestClient

from deckly.config import Settings
from deckly.infrastructure.queue import create_queue_pool
from deckly.main import API_PREFIX, create_app
from tests.integration.conftest import Cleanup, redis_settings
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
    pool = await create_queue_pool(redis_settings(settings))
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


def test_p95_latency_is_within_budget(client: TestClient, cleanup: Cleanup) -> None:
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
