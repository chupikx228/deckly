import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis

from deckly.config import Settings
from deckly.infrastructure.transcription.cache import (
    RedisTranscriptCache,
    TranscriptCacheLimits,
    dump_transcript,
    transcript_key,
)
from deckly.infrastructure.transcription.client import BYTES_PER_SECOND, DecodedAudio, Transcript
from deckly.worker.settings import build_transcript_cache
from tests.fake_process import fake_program
from tests.fakes import fresh_metrics
from tests.infrastructure.test_recording_transcriber import lecture
from tests.integration.conftest import open_queue_pool
from tests.transcription import ScriptedTranscriptionClient, caching, recording, transcriber_with

pytestmark = pytest.mark.integration

SHORT_TTL_SECONDS = 1
EXPIRY_WAIT_SECONDS = SHORT_TTL_SECONDS + 0.5
COMMAND_TIMEOUT_SECONDS = 2.0
TRANSCRIPTION_SECONDS = 0.4
DECODED_SECONDS = 12
TRANSCRIPT = lecture((0, 30), (30, 61))


def limits(ttl_seconds: int) -> TranscriptCacheLimits:
    return TranscriptCacheLimits(ttl_seconds=ttl_seconds, command_timeout_seconds=COMMAND_TIMEOUT_SECONDS)


@dataclass
class SlowTranscriptionClient(ScriptedTranscriptionClient):
    async def transcribe(self, audio: DecodedAudio) -> Transcript:
        await asyncio.sleep(TRANSCRIPTION_SECONDS)
        return await super().transcribe(audio)


@pytest.fixture
async def pool(settings: Settings) -> AsyncIterator[ArqRedis]:
    redis = await open_queue_pool(settings)
    try:
        yield redis
    finally:
        await redis.aclose()


@pytest.fixture
def documents(settings: Settings) -> Iterator[list[UUID]]:
    document_ids: list[UUID] = []
    yield document_ids

    async def purge() -> None:
        redis = await open_queue_pool(settings)
        try:
            for document_id in document_ids:
                await redis.delete(transcript_key(document_id))
        finally:
            await redis.aclose()

    asyncio.run(purge())


def unique_document(documents: list[UUID]) -> UUID:
    document_id = uuid4()
    documents.append(document_id)
    return document_id


@pytest.mark.anyio
async def test_a_stored_transcript_is_read_back_equal(pool: ArqRedis, documents: list[UUID]) -> None:
    cache = RedisTranscriptCache(pool, limits(60), metrics=fresh_metrics())
    document_id = unique_document(documents)

    await cache.put(document_id, TRANSCRIPT)

    assert await cache.get(document_id) == TRANSCRIPT


@pytest.mark.anyio
async def test_a_document_that_was_never_stored_is_a_miss_and_counted(
    pool: ArqRedis, documents: list[UUID]
) -> None:
    metrics = fresh_metrics()
    cache = RedisTranscriptCache(pool, limits(60), metrics=metrics)

    assert await cache.get(unique_document(documents)) is None

    assert metrics.registry.get_sample_value("deckly_transcript_cache_lookups_total", {"result": "miss"}) == 1


@pytest.mark.anyio
async def test_the_entry_carries_the_configured_ttl_and_it_fits_inside_the_document_retention(
    pool: ArqRedis, settings: Settings, documents: list[UUID]
) -> None:
    cache = build_transcript_cache(pool, settings, metrics=fresh_metrics())
    document_id = unique_document(documents)

    await cache.put(document_id, TRANSCRIPT)

    remaining = await pool.ttl(transcript_key(document_id))
    ttl = settings.cache.transcript_ttl_seconds
    assert ttl - 5 <= remaining <= ttl <= settings.cache.document_retention_seconds


@pytest.mark.anyio
async def test_an_entry_expires_when_its_ttl_runs_out(pool: ArqRedis, documents: list[UUID]) -> None:
    cache = RedisTranscriptCache(pool, limits(SHORT_TTL_SECONDS), metrics=fresh_metrics())
    document_id = unique_document(documents)
    await cache.put(document_id, TRANSCRIPT)
    assert await cache.get(document_id) == TRANSCRIPT

    await asyncio.sleep(EXPIRY_WAIT_SECONDS)

    assert await cache.get(document_id) is None


@pytest.mark.anyio
async def test_storing_again_restarts_the_ttl(pool: ArqRedis, documents: list[UUID]) -> None:
    cache = RedisTranscriptCache(pool, limits(60), metrics=fresh_metrics())
    document_id = unique_document(documents)
    key = transcript_key(document_id)
    await pool.set(key, b"stale", ex=5)

    await cache.put(document_id, TRANSCRIPT)

    assert await pool.ttl(key) > 5


@pytest.mark.anyio
@pytest.mark.parametrize(
    "garbage",
    [b"", b"not json", b"\xff\xfe", b"null", b"{}", b'{"language": "en", "segments": [{"x": 1}]}'],
    ids=repr,
)
async def test_a_corrupt_entry_is_a_miss_and_the_next_store_repairs_it(
    pool: ArqRedis, documents: list[UUID], garbage: bytes
) -> None:
    metrics = fresh_metrics()
    cache = RedisTranscriptCache(pool, limits(60), metrics=metrics)
    document_id = unique_document(documents)
    await pool.set(transcript_key(document_id), garbage, ex=60)

    assert await cache.get(document_id) is None
    await cache.put(document_id, TRANSCRIPT)

    assert await cache.get(document_id) == TRANSCRIPT
    assert (
        metrics.registry.get_sample_value("deckly_transcript_cache_lookups_total", {"result": "corrupt"}) == 1
    )


@pytest.mark.anyio
async def test_an_entry_written_by_one_cache_is_served_to_another_process(
    pool: ArqRedis, documents: list[UUID]
) -> None:
    document_id = unique_document(documents)
    await pool.set(transcript_key(document_id), dump_transcript(TRANSCRIPT), ex=60)

    assert (
        await RedisTranscriptCache(pool, limits(60), metrics=fresh_metrics()).get(document_id) == TRANSCRIPT
    )


@pytest.mark.anyio
async def test_second_request_for_a_document_skips_transcription_and_is_measurably_faster(
    pool: ArqRedis, documents: list[UUID], tmp_path: Path
) -> None:
    ffmpeg = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": DECODED_SECONDS * BYTES_PER_SECOND})
    client = SlowTranscriptionClient(transcript=TRANSCRIPT)
    metrics = fresh_metrics()
    transcriber = caching(
        transcriber_with(ffmpeg, client), RedisTranscriptCache(pool, limits(60), metrics=metrics)
    )
    uploaded = recording()
    documents.append(uploaded.document_id)
    await pool.delete(transcript_key(uploaded.document_id))

    started = time.perf_counter()
    first = await transcriber.parse(uploaded, b"stored recording")
    miss_seconds = time.perf_counter() - started
    started = time.perf_counter()
    second = await transcriber.parse(uploaded, b"stored recording")
    hit_seconds = time.perf_counter() - started

    assert second == first
    assert len(client.received) == 1
    assert len(ffmpeg.invocations) == 1
    assert miss_seconds >= TRANSCRIPTION_SECONDS
    assert hit_seconds < miss_seconds / 2
    assert metrics.registry.get_sample_value("deckly_transcript_cache_lookups_total", {"result": "hit"}) == 1
    assert metrics.registry.get_sample_value("deckly_transcript_cache_lookups_total", {"result": "miss"}) == 1
