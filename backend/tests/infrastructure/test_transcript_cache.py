import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.infrastructure.transcription.cache import (
    KEY_PREFIX,
    RedisTranscriptCache,
    TranscriptCacheLimits,
    dump_transcript,
    parse_payload,
    transcript_key,
)
from deckly.infrastructure.transcription.client import BYTES_PER_SECOND, Transcript, TranscriptSegment
from tests.fake_process import fake_program
from tests.fakes import fresh_metrics
from tests.infrastructure.test_queue import LOOPBACK, never_answer, unused_port
from tests.logs import captured_json_logs
from tests.transcription import ScriptedTranscriptionClient, caching, recording, segment, transcriber_with

pytestmark = pytest.mark.anyio

DOCUMENT_ID = UUID("6f1c2b9e-3d4a-4e5f-8a7b-9c0d1e2f3a4b")
SAFETY_NET_SECONDS = 5
LIMITS = TranscriptCacheLimits(ttl_seconds=60, command_timeout_seconds=1.0)
TRANSCRIPT = Transcript(
    segments=(
        TranscriptSegment(start_ms=0, end_ms=4_500, text="Привет, мир. \N{GRINNING FACE} \u200b"),
        TranscriptSegment(start_ms=4_500, end_ms=9_000, text='quote " and \\ backslash\nnewline'),
    ),
    language="ru",
)


def test_key_is_a_versioned_namespace_followed_by_the_document_id() -> None:
    assert transcript_key(DOCUMENT_ID) == f"{KEY_PREFIX}:{DOCUMENT_ID}"
    assert KEY_PREFIX.endswith(":v1")


def test_transcript_survives_a_round_trip_through_its_stored_form() -> None:
    assert parse_payload(dump_transcript(TRANSCRIPT).encode()) == TRANSCRIPT


@pytest.mark.parametrize(
    "transcript",
    [
        Transcript(segments=(), language=None),
        Transcript(segments=(), language="en"),
        Transcript(segments=(TranscriptSegment(start_ms=0, end_ms=0, text=""),), language=None),
    ],
    ids=["silence", "silence with language", "empty instant segment"],
)
def test_degenerate_transcripts_round_trip(transcript: Transcript) -> None:
    assert parse_payload(dump_transcript(transcript).encode()) == transcript


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"null",
        b"[]",
        b"{}",
        b'{"language": "en"}',
        b'{"language": "en", "segments": [{"start_ms": "0", "end_ms": 1, "text": "x"}]}',
        b'{"language": "en", "segments": [{"start_ms": 0, "end_ms": 1}]}',
        b'{"language": 7, "segments": []}',
        b'{"language": "en", "segments": [], "extra": 1}',
    ],
    ids=repr,
)
def test_a_payload_that_is_not_a_stored_transcript_cannot_be_parsed(payload: bytes) -> None:
    with pytest.raises(ValidationError):
        parse_payload(payload)


def test_a_reply_that_is_not_bytes_cannot_be_parsed() -> None:
    with pytest.raises(TypeError, match="NoneType"):
        parse_payload(None)


def test_stored_form_is_plain_json() -> None:
    stored = json.loads(dump_transcript(TRANSCRIPT))

    assert stored["language"] == "ru"
    assert stored["segments"][0] == {"start_ms": 0, "end_ms": 4_500, "text": TRANSCRIPT.segments[0].text}


def unreachable_cache() -> RedisTranscriptCache:
    redis = Redis(host=LOOPBACK, port=unused_port(), socket_connect_timeout=0.2)
    return RedisTranscriptCache(redis, LIMITS, metrics=fresh_metrics())


async def test_reading_from_an_unreachable_redis_raises_upstream_unavailable() -> None:
    with pytest.raises(UpstreamUnavailableError):
        await unreachable_cache().get(DOCUMENT_ID)


async def test_writing_to_an_unreachable_redis_raises_upstream_unavailable() -> None:
    with pytest.raises(UpstreamUnavailableError):
        await unreachable_cache().put(DOCUMENT_ID, TRANSCRIPT)


async def test_a_redis_that_accepts_the_connection_but_never_answers_is_given_up_on_within_the_timeout() -> (
    None
):
    server = await asyncio.start_server(never_answer, LOOPBACK, 0)
    port = server.sockets[0].getsockname()[1]
    redis = Redis(host=LOOPBACK, port=port)
    cache = RedisTranscriptCache(
        redis, TranscriptCacheLimits(ttl_seconds=60, command_timeout_seconds=0.1), metrics=fresh_metrics()
    )
    try:
        async with asyncio.timeout(SAFETY_NET_SECONDS):
            with pytest.raises(UpstreamUnavailableError):
                await cache.get(DOCUMENT_ID)
            with pytest.raises(UpstreamUnavailableError):
                await cache.put(DOCUMENT_ID, TRANSCRIPT)
    finally:
        await redis.aclose()
        server.close()
        await server.wait_closed()


async def test_a_failed_lookup_is_counted_as_an_error() -> None:
    metrics = fresh_metrics()
    redis = Redis(host=LOOPBACK, port=unused_port(), socket_connect_timeout=0.2)
    cache = RedisTranscriptCache(redis, LIMITS, metrics=metrics)

    with pytest.raises(UpstreamUnavailableError):
        await cache.get(DOCUMENT_ID)

    assert (
        metrics.registry.get_sample_value("deckly_transcript_cache_lookups_total", {"result": "error"}) == 1
    )


async def test_transcription_still_runs_when_the_real_cache_cannot_be_reached(tmp_path: Path) -> None:
    ffmpeg = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": 12 * BYTES_PER_SECOND})
    client = ScriptedTranscriptionClient(
        transcript=Transcript(
            segments=(segment(0, 30, "The Balkan question set the great powers against each other."),),
            language="en",
        )
    )
    transcriber = caching(transcriber_with(ffmpeg, client), unreachable_cache())

    with captured_json_logs() as logs:
        material = await transcriber.parse(recording(), b"stored recording")

    assert len(material) == 1
    assert len(client.received) == 1
    assert len(logs.named("transcript_cache_read_failed")) == 1
    assert len(logs.named("transcript_cache_write_failed")) == 1
