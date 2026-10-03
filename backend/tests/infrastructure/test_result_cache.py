import asyncio
import json
from dataclasses import replace
from typing import Unpack

import pytest
from redis.asyncio import Redis

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.result_cache import (
    KEY_PREFIX,
    RedisResultCache,
    ResultCacheLimits,
    cache_key,
    parse_payload,
)
from deckly.infrastructure.stored_result import dump_result
from tests.domain.builders import FULL_RESULT, RequestChanges
from tests.fakes import generation_request
from tests.infrastructure.test_queue import LOOPBACK, never_answer, unused_port

pytestmark = pytest.mark.anyio

SHA256_HEX_LENGTH = 64
SAFETY_NET_SECONDS = 5
LIMITS = ResultCacheLimits(ttl_seconds=60, command_timeout_seconds=1.0)


def key_of(request: GenerationRequest) -> str:
    return cache_key(request.fingerprint())


def request_with(**changes: Unpack[RequestChanges]) -> GenerationRequest:
    defaults = RequestChanges(note_types=(NoteType.BASIC, NoteType.CLOZE, NoteType.MULTIPLE_CHOICE))
    return replace(generation_request(), **(defaults | changes))


def test_key_is_a_versioned_namespace_followed_by_a_sha256_digest() -> None:
    prefix, _, digest = key_of(request_with()).rpartition(":")

    assert prefix == KEY_PREFIX
    assert len(digest) == SHA256_HEX_LENGTH
    assert set(digest) <= set("0123456789abcdef")


def test_key_never_contains_the_text_of_the_request() -> None:
    key = key_of(request_with(topic="Road signs", instructions="Only European signs"))

    assert "Road" not in key
    assert "European" not in key


def test_requests_with_the_same_normalized_tuple_share_a_key() -> None:
    canonical = request_with(topic="Road signs", language="ru")
    variants = [
        replace(canonical, note_types=(NoteType.MULTIPLE_CHOICE, NoteType.BASIC, NoteType.CLOZE)),
        replace(canonical, topic="  Road   signs\n"),
        replace(canonical, language="RU"),
        replace(canonical, instructions="   "),
    ]

    assert {key_of(variant) for variant in variants} == {key_of(canonical)}


@pytest.mark.parametrize(
    "changes",
    [
        {"topic": "Road rules"},
        {"language": "en"},
        {"card_count": 41},
        {"difficulty": Difficulty.ADVANCED},
        {"note_types": (NoteType.BASIC,)},
        {"include_images": True},
        {"instructions": "Only European signs"},
    ],
    ids=["topic", "language", "card_count", "difficulty", "note_types", "include_images", "instructions"],
)
def test_requests_differing_in_any_field_that_shapes_the_output_get_their_own_key(
    changes: RequestChanges,
) -> None:
    assert key_of(request_with(**changes)) != key_of(request_with())


def test_key_does_not_confuse_fields_that_run_into_each_other() -> None:
    assert key_of(request_with(topic="ab", language="c")) != key_of(request_with(topic="a", language="bc"))


def test_a_stored_result_is_read_back_as_the_same_result() -> None:
    payload = json.dumps(dump_result(FULL_RESULT)).encode()

    assert parse_payload(payload) == FULL_RESULT


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"null",
        b"[]",
        b"{}",
        b'{"deck": {"title": "t"}}',
        b'{"deck": {"title": ""}, "notes": []}',
        b'{"deck": {"title": "t"}, "notes": [{"unexpected": 1}]}',
        b'{"deck": {"title": "t"}, "notes": [], "extra": 1}',
        "привет".encode(),
        "привет",
        None,
        42,
    ],
    ids=repr,
)
def test_anything_that_is_not_a_stored_result_cannot_be_parsed(payload: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_payload(payload)


def test_a_stored_note_that_breaks_a_domain_invariant_cannot_be_parsed() -> None:
    document = dump_result(FULL_RESULT)
    notes = document["notes"]
    assert isinstance(notes, list)
    document["notes"] = [*notes, notes[0]]

    with pytest.raises(ValueError, match="clientId"):
        parse_payload(json.dumps(document).encode())


def unreachable_cache() -> RedisResultCache:
    redis = Redis(host=LOOPBACK, port=unused_port(), socket_connect_timeout=0.2)
    return RedisResultCache(redis, LIMITS)


async def test_reading_from_an_unreachable_redis_raises_upstream_unavailable() -> None:
    cache = unreachable_cache()

    with pytest.raises(UpstreamUnavailableError):
        await cache.get(generation_request())


async def test_writing_to_an_unreachable_redis_raises_upstream_unavailable() -> None:
    cache = unreachable_cache()

    with pytest.raises(UpstreamUnavailableError):
        await cache.put(generation_request(), FULL_RESULT)


async def test_a_redis_that_accepts_the_connection_but_never_answers_is_given_up_on_within_the_timeout() -> (
    None
):
    server = await asyncio.start_server(never_answer, LOOPBACK, 0)
    port = server.sockets[0].getsockname()[1]
    redis = Redis(host=LOOPBACK, port=port)
    cache = RedisResultCache(redis, ResultCacheLimits(ttl_seconds=60, command_timeout_seconds=0.1))
    try:
        async with asyncio.timeout(SAFETY_NET_SECONDS):
            with pytest.raises(UpstreamUnavailableError):
                await cache.get(generation_request())
            with pytest.raises(UpstreamUnavailableError):
                await cache.put(generation_request(), FULL_RESULT)
    finally:
        await redis.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "text",
    [
        "\ud800 lone surrogate",
        "\N{RIGHT-TO-LEFT OVERRIDE}RTL override",
        "\N{ZERO WIDTH SPACE}\N{ZERO WIDTH JOINER} zero width",
        "emoji \N{GRINNING FACE}",
        "a\x00b",
    ],
    ids=["surrogate", "rtl override", "zero width", "emoji", "nul"],
)
def test_a_key_can_be_derived_from_any_text_a_request_can_carry(text: str) -> None:
    key = key_of(request_with(topic=text, instructions=text))

    assert key.startswith(f"{KEY_PREFIX}:")
    assert key.isascii()
