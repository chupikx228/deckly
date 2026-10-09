import asyncio
import json
import traceback
from collections.abc import Callable
from dataclasses import replace

import httpx2
import pytest

from deckly.infrastructure.llm.client import (
    REDACTED,
    LlmEndpoint,
    LlmError,
    LlmPrompt,
    LlmRejectedError,
    LlmReply,
    LlmResponseError,
    LlmStop,
    LlmUnavailableError,
)
from deckly.infrastructure.llm.gemini_client import GeminiLlmClient
from deckly.infrastructure.resilience import TransientError

pytestmark = pytest.mark.anyio

API_KEY = "test-gemini-key-0123456789-abcdefghij-xq7z"
ENDPOINT = LlmEndpoint(
    base_url="https://generativelanguage.test/v1beta",
    api_key=API_KEY,
    model="gemini-test",
    max_output_tokens=4321,
    timeout_seconds=5,
)
PROMPT = LlmPrompt(system="system instructions", user="user request", expected_output_tokens=100)
RETRY_INFO = "type.googleapis.com/google.rpc.RetryInfo"
DEEPLY_NESTED = b"[" * 100_000 + b"]" * 100_000

type Handler = Callable[[httpx2.Request], httpx2.Response]


def candidate(
    parts: list[dict[str, object]] | None = None, finish_reason: object = "STOP"
) -> dict[str, object]:
    return {
        "content": {"role": "model", "parts": [{"text": "{}"}] if parts is None else parts},
        "finishReason": finish_reason,
        "index": 0,
    }


def generated(*candidates: dict[str, object]) -> dict[str, object]:
    return {
        "candidates": list(candidates) or [candidate()],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
        "modelVersion": "gemini-test",
    }


def google_error(status: int, message: str, details: list[object] | None = None) -> dict[str, object]:
    return {"error": {"code": status, "message": message, "status": "STATUS", "details": details or []}}


def answering(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda _: httpx2.Response(status, json=body, headers=headers)


def raising(error: Exception) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        raise error

    return handler


async def complete_with(handler: Handler, endpoint: LlmEndpoint = ENDPOINT) -> LlmReply:
    client = GeminiLlmClient(endpoint, httpx2.MockTransport(handler))
    try:
        return await client.complete(PROMPT)
    finally:
        await client.aclose()


async def test_request_carries_the_key_in_a_header_and_asks_for_json_with_low_thinking() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=generated())

    await complete_with(handler)

    [request] = seen
    assert (request.method, request.url.path) == ("POST", "/v1beta/models/gemini-test:generateContent")
    assert request.headers["x-goog-api-key"] == API_KEY
    assert not request.url.query
    assert API_KEY not in str(request.url)
    assert json.loads(request.content) == {
        "systemInstruction": {"parts": [{"text": "system instructions"}]},
        "contents": [{"role": "user", "parts": [{"text": "user request"}]}],
        "generationConfig": {
            "candidateCount": 1,
            "maxOutputTokens": 4321,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }


@pytest.mark.parametrize(
    "base_url", ["https://gateway.test/gemini/v1beta", "https://gateway.test/gemini/v1beta/"]
)
async def test_base_url_with_a_path_prefix_keeps_the_prefix(base_url: str) -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(200, json=generated())

    await complete_with(handler, replace(ENDPOINT, base_url=base_url))

    assert seen == ["/gemini/v1beta/models/gemini-test:generateContent"]


@pytest.mark.parametrize("model", ["../../files", "gemini?alt=sse", "gemini#x", "a/b:streamGenerateContent"])
async def test_model_name_cannot_change_the_endpoint_called(model: str) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=generated())

    await complete_with(handler, replace(ENDPOINT, model=model))

    [request] = seen
    assert request.url.raw_path.startswith(b"/v1beta/models/")
    assert request.url.raw_path.endswith(b":generateContent")
    assert request.url.raw_path.count(b"/") == 3
    assert not request.url.query
    assert not request.url.fragment


async def test_text_parts_are_joined_and_thoughts_and_other_parts_ignored() -> None:
    parts: list[dict[str, object]] = [
        {"text": "thinking about it", "thought": True},
        {"text": '{"notes": '},
        {"functionCall": {"name": "f", "args": {}}},
        {"text": "[]}"},
    ]

    reply = await complete_with(answering(generated(candidate(parts))))

    assert reply == LlmReply(text='{"notes": []}', stop=LlmStop.COMPLETE)


async def test_only_the_first_candidate_is_used() -> None:
    first = candidate([{"text": "first"}])
    second = candidate([{"text": "second"}], "SAFETY")

    reply = await complete_with(answering(generated(first, second)))

    assert reply == LlmReply(text="first", stop=LlmStop.COMPLETE)


FINISH_REASONS: dict[str, tuple[object, LlmStop]] = {
    "stop": ("STOP", LlmStop.COMPLETE),
    "max tokens": ("MAX_TOKENS", LlmStop.TRUNCATED),
    "safety": ("SAFETY", LlmStop.REFUSED),
    "recitation": ("RECITATION", LlmStop.REFUSED),
    "language": ("LANGUAGE", LlmStop.REFUSED),
    "other": ("OTHER", LlmStop.REFUSED),
    "blocklist": ("BLOCKLIST", LlmStop.REFUSED),
    "prohibited content": ("PROHIBITED_CONTENT", LlmStop.REFUSED),
    "spii": ("SPII", LlmStop.REFUSED),
    "image safety": ("IMAGE_SAFETY", LlmStop.REFUSED),
    "image prohibited content": ("IMAGE_PROHIBITED_CONTENT", LlmStop.REFUSED),
    "escalation": ("ESCALATION", LlmStop.REFUSED),
    "unknown future reason": ("SOMETHING_NEW", LlmStop.COMPLETE),
    "unspecified": ("FINISH_REASON_UNSPECIFIED", LlmStop.COMPLETE),
    "missing": (None, LlmStop.COMPLETE),
}


@pytest.mark.parametrize(("finish_reason", "stop"), FINISH_REASONS.values(), ids=FINISH_REASONS.keys())
async def test_finish_reason_maps_to_how_the_reply_ended(finish_reason: object, stop: LlmStop) -> None:
    reply = await complete_with(answering(generated(candidate(finish_reason=finish_reason))))

    assert reply.stop is stop


SAFETY_STOPPED_CANDIDATES: dict[str, dict[str, object]] = {
    "no content": {"finishReason": "SAFETY", "index": 0},
    "content without parts": {"content": {"role": "model"}, "finishReason": "SAFETY"},
    "null content": {"content": None, "finishReason": "SAFETY"},
}


@pytest.mark.parametrize("body", SAFETY_STOPPED_CANDIDATES.values(), ids=SAFETY_STOPPED_CANDIDATES.keys())
async def test_candidate_stopped_for_safety_without_content_is_an_empty_refusal(
    body: dict[str, object],
) -> None:
    reply = await complete_with(answering({"candidates": [body]}))

    assert reply == LlmReply(text="", stop=LlmStop.REFUSED)


@pytest.mark.parametrize(
    "block_reason", ["SAFETY", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "NEW_REASON"]
)
async def test_blocked_prompt_is_a_refusal(block_reason: str) -> None:
    body = {"promptFeedback": {"blockReason": block_reason, "safetyRatings": []}}

    reply = await complete_with(answering(body))

    assert reply == LlmReply(text="", stop=LlmStop.REFUSED)


async def test_blocked_prompt_wins_over_any_candidate() -> None:
    body = {**generated(), "promptFeedback": {"blockReason": "SAFETY"}}

    reply = await complete_with(answering(body))

    assert reply.stop is LlmStop.REFUSED


async def test_suspended_account_is_rejected_instead_of_refusing_every_prompt() -> None:
    with pytest.raises(LlmRejectedError) as raised:
        await complete_with(answering(generated(candidate([], "PUP_LIMITED_DISABLED"))))

    assert not isinstance(raised.value, TransientError)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
async def test_transient_status_is_reported_as_unavailable(status: int) -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(answering(google_error(status, "busy"), status))


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 416, 499])
async def test_rejected_request_is_not_reported_as_transient(status: int) -> None:
    with pytest.raises(LlmRejectedError) as raised:
        await complete_with(answering(google_error(status, "rejected"), status))

    assert not isinstance(raised.value, TransientError)
    assert API_KEY not in str(raised.value)


async def test_retry_after_header_is_passed_on_with_the_transient_failure() -> None:
    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(answering(google_error(429, "slow down"), 429, {"retry-after": "3"}))

    assert raised.value.retry_after_seconds == 3.0


RETRY_DELAYS: dict[str, tuple[str, float]] = {
    "whole seconds": ("25s", 25.0),
    "fractional seconds": ("1.500s", 1.5),
    "zero": ("0s", 0.0),
}


@pytest.mark.parametrize(("delay", "seconds"), RETRY_DELAYS.values(), ids=RETRY_DELAYS.keys())
async def test_retry_delay_in_the_error_details_is_passed_on_when_there_is_no_header(
    delay: str, seconds: float
) -> None:
    details: list[object] = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
        {"@type": RETRY_INFO, "retryDelay": delay},
    ]

    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(answering(google_error(429, "quota", details), 429))

    assert raised.value.retry_after_seconds == seconds


async def test_retry_after_header_wins_over_the_retry_delay_in_the_body() -> None:
    details: list[object] = [{"@type": RETRY_INFO, "retryDelay": "40s"}]

    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(answering(google_error(429, "quota", details), 429, {"retry-after": "2"}))

    assert raised.value.retry_after_seconds == 2.0


async def test_unusable_retry_after_header_falls_back_to_the_retry_delay_in_the_body() -> None:
    details: list[object] = [{"@type": RETRY_INFO, "retryDelay": "7s"}]

    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(answering(google_error(429, "quota", details), 429, {"retry-after": "soon"}))

    assert raised.value.retry_after_seconds == 7.0


UNUSABLE_RETRY_HINTS: dict[str, Handler] = {
    "header date": answering(
        google_error(503, "busy"), 503, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
    ),
    "negative header": answering(google_error(503, "busy"), 503, {"retry-after": "-5"}),
    "nan header": answering(google_error(503, "busy"), 503, {"retry-after": "nan"}),
    "delay without unit": answering(google_error(429, "q", [{"@type": RETRY_INFO, "retryDelay": "25"}]), 429),
    "delay in minutes": answering(google_error(429, "q", [{"@type": RETRY_INFO, "retryDelay": "1m"}]), 429),
    "infinite delay": answering(google_error(429, "q", [{"@type": RETRY_INFO, "retryDelay": "infs"}]), 429),
    "negative delay": answering(google_error(429, "q", [{"@type": RETRY_INFO, "retryDelay": "-3s"}]), 429),
    "delay that is not a string": answering(
        google_error(429, "q", [{"@type": RETRY_INFO, "retryDelay": 25}]), 429
    ),
    "delay from another detail type": answering(
        google_error(429, "q", [{"@type": "type.googleapis.com/x", "retryDelay": "25s"}]), 429
    ),
    "details that are not a list": answering({"error": {"code": 429, "details": "soon"}}, 429),
    "body that is not json": lambda _: httpx2.Response(503, content=b"<html>busy</html>"),
    "empty body": lambda _: httpx2.Response(503),
    "body nested too deeply to parse": lambda _: httpx2.Response(503, content=DEEPLY_NESTED),
}


@pytest.mark.parametrize("handler", UNUSABLE_RETRY_HINTS.values(), ids=UNUSABLE_RETRY_HINTS.keys())
async def test_retry_hint_that_is_not_a_usable_number_of_seconds_is_ignored(handler: Handler) -> None:
    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(handler)

    assert raised.value.retry_after_seconds is None


KEY_ECHOES: dict[str, tuple[int, str]] = {
    "whole key on an auth failure": (400, API_KEY),
    "whole key on a permission failure": (403, API_KEY),
    "whole key on a transient failure": (503, API_KEY),
    "masked key": (400, f"{API_KEY[:6]}****{API_KEY[-4:]}"),
}


@pytest.mark.parametrize(("status", "shown"), KEY_ECHOES.values(), ids=KEY_ECHOES.keys())
async def test_key_echoed_in_an_error_body_is_redacted_before_the_error_can_be_logged(
    status: int, shown: str
) -> None:
    body = google_error(status, f"API key not valid: {shown}. Please pass a valid API key.")

    with pytest.raises(LlmError) as raised:
        await complete_with(answering(body, status))

    logged = "".join(traceback.format_exception(raised.value))
    assert f"API key not valid: {REDACTED}." in logged
    assert API_KEY[6:-4] not in logged
    assert API_KEY[-4:] not in logged


@pytest.mark.parametrize(
    "error",
    [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow"), httpx2.RemoteProtocolError("closed")],
    ids=["connect error", "read timeout", "connection dropped"],
)
async def test_network_failure_is_reported_as_unavailable(error: Exception) -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(raising(error))


async def test_failure_to_decode_the_response_is_a_response_error() -> None:
    with pytest.raises(LlmResponseError):
        await complete_with(raising(httpx2.DecodingError("bad gzip")))


async def test_network_failure_never_carries_the_key_into_the_logged_traceback() -> None:
    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(raising(httpx2.ConnectError("refused")))

    assert API_KEY not in "".join(traceback.format_exception(raised.value))


MALFORMED_BODIES: dict[str, Handler] = {
    "not json": lambda _: httpx2.Response(200, content=b"<html>gateway</html>"),
    "empty body": lambda _: httpx2.Response(200),
    "json without candidates": answering({"usageMetadata": {}}),
    "empty candidates": answering({"candidates": []}),
    "candidate that is not an object": answering({"candidates": ["text"]}),
    "unspecified block reason without candidates": answering(
        {"promptFeedback": {"blockReason": "BLOCK_REASON_UNSPECIFIED"}}
    ),
    "content that is not an object": answering({"candidates": [{"content": "text", "finishReason": "STOP"}]}),
    "parts that are not a list": answering({"candidates": [{"content": {"parts": {"text": "x"}}}]}),
    "part that is not an object": answering(generated(candidate(parts=None) | {"content": {"parts": ["x"]}})),
    "text that is not a string": answering(generated(candidate([{"text": 5}]))),
    "finish reason that is not a string": answering(generated(candidate(finish_reason=1))),
    "json array": answering([generated()]),
}


@pytest.mark.parametrize("handler", MALFORMED_BODIES.values(), ids=MALFORMED_BODIES.keys())
async def test_malformed_response_envelope_is_a_response_error(handler: Handler) -> None:
    with pytest.raises(LlmResponseError):
        await complete_with(handler)


class SlowProvider:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.endings: list[str] = []

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        del request
        self.reached.set()
        try:
            await asyncio.sleep(ENDPOINT.timeout_seconds)
        except asyncio.CancelledError:
            self.endings.append("aborted")
            raise
        self.endings.append("answered")
        return httpx2.Response(200, json=generated())


async def test_cancelling_the_caller_aborts_the_request_in_flight() -> None:
    provider = SlowProvider()
    client = GeminiLlmClient(ENDPOINT, httpx2.MockTransport(provider.handle))
    try:
        call = asyncio.create_task(client.complete(PROMPT))
        await provider.reached.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        await client.aclose()

    assert provider.endings == ["aborted"]
