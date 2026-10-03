import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from http import HTTPStatus
from typing import Unpack
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from fastapi.testclient import TestClient

from deckly.application.pipeline import RunGeneration
from deckly.config import Settings
from deckly.domain.generation import GenerationRequest
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.result_cache import RedisResultCache, ResultCacheLimits, cache_key
from deckly.main import API_PREFIX, create_app
from deckly.worker.settings import build_result_cache
from tests.domain.builders import FULL_RESULT, RequestChanges
from tests.fakes import GENERATED, FakeProviders, generation_request
from tests.integration.conftest import Cleanup, open_queue_pool
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.integration

ENDPOINT = f"{API_PREFIX}/generations"
SHORT_TTL_SECONDS = 1
EXPIRY_WAIT_SECONDS = SHORT_TTL_SECONDS + 0.5
COMMAND_TIMEOUT_SECONDS = 2.0


def limits(ttl_seconds: int) -> ResultCacheLimits:
    return ResultCacheLimits(ttl_seconds=ttl_seconds, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS)


def unique_request(**changes: Unpack[RequestChanges]) -> GenerationRequest:
    return replace(generation_request(topic=f"Road signs {uuid4()}"), **changes)


@pytest.fixture
async def pool(settings: Settings) -> AsyncIterator[ArqRedis]:
    redis = await open_queue_pool(settings)
    try:
        yield redis
    finally:
        await redis.aclose()


@pytest.fixture
def entries(settings: Settings) -> Iterator[list[GenerationRequest]]:
    requests: list[GenerationRequest] = []
    yield requests

    async def purge() -> None:
        redis = await open_queue_pool(settings)
        try:
            for request in requests:
                await redis.delete(cache_key(request.fingerprint()))
        finally:
            await redis.aclose()

    asyncio.run(purge())


@pytest.mark.anyio
async def test_a_stored_result_is_read_back_equal_for_an_equivalent_request(
    pool: ArqRedis, entries: list[GenerationRequest]
) -> None:
    cache = RedisResultCache(pool, limits(60))
    request = unique_request(language="ru")
    entries.append(request)

    await cache.put(request, FULL_RESULT)

    assert await cache.get(replace(request, language="RU", topic=f"  {request.topic}\n")) == FULL_RESULT


@pytest.mark.anyio
async def test_a_request_that_was_never_stored_is_a_miss(pool: ArqRedis) -> None:
    assert await RedisResultCache(pool, limits(60)).get(unique_request()) is None


@pytest.mark.anyio
async def test_the_entry_carries_the_configured_ttl(
    pool: ArqRedis, settings: Settings, entries: list[GenerationRequest]
) -> None:
    cache = build_result_cache(pool, settings)
    request = unique_request()
    entries.append(request)

    await cache.put(request, GENERATED)

    remaining = await pool.ttl(cache_key(request.fingerprint()))
    assert (
        settings.cache.generation_result_ttl_seconds - 5
        <= remaining
        <= (settings.cache.generation_result_ttl_seconds)
    )


@pytest.mark.anyio
async def test_an_entry_expires_when_its_ttl_runs_out(
    pool: ArqRedis, entries: list[GenerationRequest]
) -> None:
    cache = RedisResultCache(pool, limits(SHORT_TTL_SECONDS))
    request = unique_request()
    entries.append(request)
    await cache.put(request, GENERATED)
    assert await cache.get(request) == GENERATED

    await asyncio.sleep(EXPIRY_WAIT_SECONDS)

    assert await cache.get(request) is None


@pytest.mark.anyio
async def test_storing_again_restarts_the_ttl(pool: ArqRedis, entries: list[GenerationRequest]) -> None:
    cache = RedisResultCache(pool, limits(60))
    request = unique_request()
    entries.append(request)
    key = cache_key(request.fingerprint())
    await pool.set(key, b"stale", ex=5)

    await cache.put(request, GENERATED)

    assert await pool.ttl(key) > 5


@pytest.mark.anyio
@pytest.mark.parametrize(
    "garbage",
    [b"", b"not json", b"\xff\xfe", b"null", b"{}", b'{"deck": {"title": "t"}, "notes": [{"x": 1}]}'],
    ids=repr,
)
async def test_a_corrupt_entry_is_a_miss_and_the_next_store_repairs_it(
    pool: ArqRedis, entries: list[GenerationRequest], garbage: bytes
) -> None:
    cache = RedisResultCache(pool, limits(60))
    request = unique_request()
    entries.append(request)
    await pool.set(cache_key(request.fingerprint()), garbage, ex=60)

    assert await cache.get(request) is None

    await cache.put(request, GENERATED)
    assert await cache.get(request) == GENERATED


@pytest.mark.anyio
async def test_an_entry_holds_the_validated_domain_result_and_nothing_else(
    pool: ArqRedis, entries: list[GenerationRequest]
) -> None:
    cache = RedisResultCache(pool, limits(60))
    request = unique_request()
    entries.append(request)

    await cache.put(request, FULL_RESULT)

    stored = await pool.get(cache_key(request.fingerprint()))
    assert isinstance(stored, bytes)
    assert set(json.loads(stored)) == {"deck", "notes"}


@pytest.mark.anyio
async def test_requests_that_differ_only_in_what_shapes_the_output_do_not_share_an_entry(
    pool: ArqRedis, entries: list[GenerationRequest]
) -> None:
    cache = RedisResultCache(pool, limits(60))
    request = unique_request()
    entries.append(request)
    await cache.put(request, GENERATED)

    assert await cache.get(replace(request, include_images=True)) is None
    assert await cache.get(replace(request, instructions="Only European signs")) is None
    assert await cache.get(replace(request, note_types=(NoteType.BASIC, NoteType.CLOZE))) is None


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        root.handlers, root.level = handlers, level


async def run_through_postgres_and_redis(settings: Settings, providers: FakeProviders, job_id: UUID) -> None:
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    redis = await open_queue_pool(settings)
    try:
        await RunGeneration(
            store=PostgresJobStore(create_session_factory(engine)),
            cache=build_result_cache(redis, settings),
            retriever=providers,
            parser=providers,
            generator=providers,
            media=providers,
            clock=utc_now,
        )(job_id)
    finally:
        await redis.aclose()
        await engine.dispose()


def submit(client: TestClient, cleanup: Cleanup, payload: dict[str, object]) -> UUID:
    client_id = uuid4()
    cleanup.client_ids.add(client_id)
    created = client.post(
        ENDPOINT, json=payload, headers={"Idempotency-Key": str(uuid4()), "X-Client-Id": str(client_id)}
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    job_id = UUID(created.json()["jobId"])
    cleanup.job_ids.add(job_id)
    return job_id


def poll(client: TestClient, job_id: UUID) -> dict[str, object]:
    response = client.get(f"{ENDPOINT}/{job_id}", headers={"X-Client-Id": str(uuid4())})
    assert response.status_code == HTTPStatus.OK
    body: dict[str, object] = response.json()
    assert spec_errors("GenerationJob", body) == []
    return body


def test_a_second_client_asking_for_the_same_deck_polls_a_succeeded_job_served_from_the_cache(
    client: TestClient, cleanup: Cleanup, settings: Settings, entries: list[GenerationRequest]
) -> None:
    topic = f"Road signs {uuid4()}"
    entries.append(
        replace(
            generation_request(topic=topic),
            language="ru",
            card_count=40,
            note_types=(NoteType.BASIC, NoteType.CLOZE),
        )
    )
    providers = FakeProviders()
    first = submit(
        client,
        cleanup,
        {"topic": topic, "language": "ru", "cardCount": 40, "noteTypes": ["cloze", "basic"]},
    )
    asyncio.run(run_through_postgres_and_redis(settings, providers, first))
    generated_for_the_first = list(providers.generated_for)

    second = submit(
        client,
        cleanup,
        {"topic": f"  {topic}  ", "language": "RU", "cardCount": 40, "noteTypes": ["basic", "cloze"]},
    )
    queued = poll(client, second)
    asyncio.run(run_through_postgres_and_redis(settings, providers, second))
    served = poll(client, second)

    assert queued["status"] == "queued"
    assert (served["status"], served["stage"], served["progress"], served["error"]) == (
        "succeeded",
        None,
        1.0,
        None,
    )
    assert served["result"] == poll(client, first)["result"]
    assert providers.generated_for == generated_for_the_first == [first]
    assert providers.retrieved_for == [first]


def test_a_request_for_images_is_not_served_the_deck_cached_without_them(
    client: TestClient, cleanup: Cleanup, settings: Settings, entries: list[GenerationRequest]
) -> None:
    topic = f"Road signs {uuid4()}"
    plain = replace(generation_request(topic=topic), language="ru", card_count=40)
    entries.extend([plain, replace(plain, include_images=True)])
    providers = FakeProviders()
    payload: dict[str, object] = {"topic": topic, "language": "ru", "cardCount": 40}
    first = submit(client, cleanup, payload)
    asyncio.run(run_through_postgres_and_redis(settings, providers, first))

    second = submit(client, cleanup, {**payload, "includeImages": True})
    asyncio.run(run_through_postgres_and_redis(settings, providers, second))

    assert providers.generated_for == [first, second]
    assert providers.fetched_for == [second]
