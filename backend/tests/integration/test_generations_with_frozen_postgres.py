import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.config import Settings
from deckly.domain.job import GenerationJob
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.main import API_PREFIX, create_app
from deckly.transport.generations import CLIENT_ID_HEADER, IDEMPOTENCY_KEY_HEADER
from tests.domain.builders import T0
from tests.fakes import generation_request
from tests.integration.conftest import Cleanup
from tests.integration.test_health_endpoint import Dependency, Proxy, postgres_at, upstream
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ENDPOINT = f"{API_PREFIX}/generations"
PAYLOAD = {"topic": "Road signs", "language": "ru", "cardCount": 40}
JOB_STORE_TIMEOUT_SECONDS = 0.5
SAFETY_NET_SECONDS = 10
RECOVERY_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class Service:
    client: httpx2.AsyncClient
    postgres: Proxy
    cleanup: Cleanup
    job_id: UUID


type Call = Callable[[Service], Awaitable[httpx2.Response]]


def through_proxy(settings: Settings, postgres_port: int) -> Settings:
    return replace(
        settings,
        database=settings.database.model_copy(
            update={
                "url": postgres_at(settings, postgres_port),
                "job_store_timeout_seconds": JOB_STORE_TIMEOUT_SECONDS,
            }
        ),
    )


async def stored_job(session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup) -> UUID:
    job = GenerationJob.queue(uuid4(), T0)
    cleanup.job_ids.add(job.job_id)
    await PostgresJobStore(session_factory).add(job, generation_request(), cleanup.scope())
    return job.job_id


@pytest.fixture
async def service(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    restored_logging: None,
) -> AsyncIterator[Service]:
    del restored_logging
    cleanup = Cleanup(settings)
    postgres = upstream(settings, Dependency.POSTGRES)
    app = create_app(through_proxy(settings, await postgres.start()))
    try:
        job_id = await stored_job(session_factory, cleanup)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app, raise_app_exceptions=False), base_url="http://deckly"
            ) as client,
        ):
            try:
                yield Service(client=client, postgres=postgres, cleanup=cleanup, job_id=job_id)
            finally:
                postgres.thaw()
    finally:
        await postgres.cut()
        await cleanup.purge()


def client_headers(service: Service) -> dict[str, str]:
    client_id = uuid4()
    service.cleanup.client_ids.add(client_id)
    return {CLIENT_ID_HEADER: str(client_id)}


async def create(service: Service) -> httpx2.Response:
    headers = {**client_headers(service), IDEMPOTENCY_KEY_HEADER: str(uuid4())}
    response = await service.client.post(ENDPOINT, json=PAYLOAD, headers=headers)
    if response.status_code == HTTPStatus.ACCEPTED:
        service.cleanup.job_ids.add(UUID(response.json()["jobId"]))
    return response


async def poll(service: Service) -> httpx2.Response:
    return await service.client.get(f"{ENDPOINT}/{service.job_id}", headers=client_headers(service))


async def cancel(service: Service) -> httpx2.Response:
    return await service.client.post(f"{ENDPOINT}/{service.job_id}/cancel", headers=client_headers(service))


CALLS: dict[str, tuple[Call, HTTPStatus]] = {
    "create": (create, HTTPStatus.ACCEPTED),
    "poll": (poll, HTTPStatus.OK),
    "cancel": (cancel, HTTPStatus.NO_CONTENT),
}


async def timed(service: Service, call: Call) -> tuple[httpx2.Response, float]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(SAFETY_NET_SECONDS):
        response = await call(service)
    return response, loop.time() - started


async def until_answered(service: Service) -> httpx2.Response:
    async with asyncio.timeout(SAFETY_NET_SECONDS):
        while True:
            response = await poll(service)
            if response.status_code == HTTPStatus.OK:
                return response
            await asyncio.sleep(RECOVERY_POLL_SECONDS)


def assert_internal_error(response: httpx2.Response) -> None:
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR, response.text
    body: dict[str, object] = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == "INTERNAL_ERROR"


@pytest.mark.parametrize("name", list(CALLS))
async def test_frozen_postgres_fails_the_request_within_its_bound_and_it_recovers(
    service: Service, name: str
) -> None:
    call, answered = CALLS[name]
    await until_answered(service)
    service.postgres.freeze()

    first, first_elapsed = await timed(service, call)
    second, second_elapsed = await timed(service, call)

    assert_internal_error(first)
    assert first_elapsed < JOB_STORE_TIMEOUT_SECONDS + 1
    assert_internal_error(second)
    assert second_elapsed < JOB_STORE_TIMEOUT_SECONDS + 1

    service.postgres.thaw()

    assert (await until_answered(service)).json()["status"] == "queued"
    assert (await call(service)).status_code == answered


async def test_frozen_postgres_does_not_exhaust_the_pool_for_good(
    service: Service, settings: Settings
) -> None:
    await until_answered(service)
    service.postgres.freeze()

    pool_capacity = settings.database.pool_size + settings.database.max_overflow
    for _ in range(pool_capacity + 1):
        response, elapsed = await timed(service, poll)
        assert_internal_error(response)
        assert elapsed < JOB_STORE_TIMEOUT_SECONDS + 1

    service.postgres.thaw()

    responses = [await until_answered(service) for _ in range(pool_capacity + 1)]
    assert {response.status_code for response in responses} == {HTTPStatus.OK}
