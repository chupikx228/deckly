import asyncio
import json
import traceback
from collections.abc import Callable
from dataclasses import replace

import httpx2
import pytest

from deckly.infrastructure.llm.client import (
    LlmEndpoint,
    LlmError,
    LlmPrompt,
    LlmRejectedError,
    LlmReply,
    LlmResponseError,
    LlmStop,
    LlmUnavailableError,
)
from deckly.infrastructure.llm.deepseek_client import REDACTED, DeepSeekLlmClient
from deckly.infrastructure.resilience import TransientError

pytestmark = pytest.mark.anyio

API_KEY = "test-deepseek-key"
ENDPOINT = LlmEndpoint(
    base_url="https://api.deepseek.test",
    api_key=API_KEY,
    model="deepseek-test",
    max_output_tokens=4321,
    timeout_seconds=5,
)
PROMPT = LlmPrompt(system="system instructions", user="user request", expected_output_tokens=100)

type Handler = Callable[[httpx2.Request], httpx2.Response]


def completion(content: object = "{}", finish_reason: object = "stop") -> dict[str, object]:
    return {
        "id": "completion-1",
        "object": "chat.completion",
        "model": "deepseek-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def answering(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda _: httpx2.Response(status, json=body, headers=headers)


def raising(error: Exception) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        raise error

    return handler


async def complete_with(handler: Handler, endpoint: LlmEndpoint = ENDPOINT) -> LlmReply:
    client = DeepSeekLlmClient(endpoint, httpx2.MockTransport(handler))
    try:
        return await client.complete(PROMPT)
    finally:
        await client.aclose()


async def test_request_asks_for_json_output_with_the_prompt_model_and_token_cap() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=completion())

    await complete_with(handler)

    [request] = seen
    assert (request.method, request.url.path) == ("POST", "/chat/completions")
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {
        "model": "deepseek-test",
        "messages": [
            {"role": "system", "content": "system instructions"},
            {"role": "user", "content": "user request"},
        ],
        "max_tokens": 4321,
        "response_format": {"type": "json_object"},
        "stream": False,
    }


@pytest.mark.parametrize("base_url", ["https://gateway.test/deepseek", "https://gateway.test/deepseek/"])
async def test_base_url_with_a_path_prefix_keeps_the_prefix(base_url: str) -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(200, json=completion())

    await complete_with(handler, replace(ENDPOINT, base_url=base_url))

    assert seen == ["/deepseek/chat/completions"]


async def test_keep_alive_blank_lines_before_the_json_body_are_tolerated() -> None:
    body = b"\n\n\n" + json.dumps(completion('{"notes": []}')).encode()

    reply = await complete_with(lambda _: httpx2.Response(200, content=body))

    assert reply == LlmReply(text='{"notes": []}', stop=LlmStop.COMPLETE)


FINISH_REASONS: dict[str, tuple[object, LlmStop]] = {
    "stop": ("stop", LlmStop.COMPLETE),
    "length": ("length", LlmStop.TRUNCATED),
    "content filter": ("content_filter", LlmStop.REFUSED),
    "tool calls": ("tool_calls", LlmStop.COMPLETE),
    "missing": (None, LlmStop.COMPLETE),
}


@pytest.mark.parametrize(("finish_reason", "stop"), FINISH_REASONS.values(), ids=FINISH_REASONS.keys())
async def test_finish_reason_maps_to_how_the_reply_ended(finish_reason: object, stop: LlmStop) -> None:
    reply = await complete_with(answering(completion(finish_reason=finish_reason)))

    assert reply.stop is stop


@pytest.mark.parametrize("finish_reason", ["insufficient_system_resource", "aborted"])
async def test_finish_reason_signalling_provider_capacity_is_unavailable(finish_reason: str) -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(answering(completion(finish_reason=finish_reason)))


async def test_keep_alive_lines_without_a_completion_are_a_transient_failure() -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(lambda _: httpx2.Response(200, content=b"\n\n\n"))


@pytest.mark.parametrize("retry_after", ["Wed, 21 Oct 2026 07:28:00 GMT", "-5", "nan", "inf", ""])
async def test_retry_after_that_is_not_a_usable_number_of_seconds_is_ignored(retry_after: str) -> None:
    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(answering({"error": {"message": "busy"}}, 503, {"retry-after": retry_after}))

    assert raised.value.retry_after_seconds is None


async def test_empty_content_is_an_empty_reply_not_an_error() -> None:
    reply = await complete_with(answering(completion(content=None)))

    assert reply == LlmReply(text="", stop=LlmStop.COMPLETE)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503])
async def test_transient_status_is_reported_as_unavailable(status: int) -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(answering({"error": {"message": "busy"}}, status))


async def test_retry_after_header_is_passed_on_with_the_transient_failure() -> None:
    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(answering({"error": {"message": "slow down"}}, 429, {"retry-after": "3"}))

    assert raised.value.retry_after_seconds == 3.0


@pytest.mark.parametrize("status", [400, 401, 402, 404, 422])
async def test_rejected_request_is_not_reported_as_transient(status: int) -> None:
    with pytest.raises(LlmRejectedError) as raised:
        await complete_with(answering({"error": {"message": "rejected"}}, status))

    assert not isinstance(raised.value, TransientError)
    assert API_KEY not in str(raised.value)


ECHOED_KEY = "sk-5b1e0c9a7d3f4e2b8a6c1d0e9f7a3b2c"
KEY_ECHOES: dict[str, tuple[int, str]] = {
    "masked key suffix": (401, f"****{ECHOED_KEY[-4:]}"),
    "masked key that keeps its leading characters": (401, f"{ECHOED_KEY[:7]}****{ECHOED_KEY[-4:]}"),
    "whole key": (401, ECHOED_KEY),
    "masked key on a transient failure": (503, f"****{ECHOED_KEY[-4:]}"),
}


@pytest.mark.parametrize(("status", "shown"), KEY_ECHOES.values(), ids=KEY_ECHOES.keys())
async def test_key_echoed_in_an_error_body_is_redacted_before_the_error_can_be_logged(
    status: int, shown: str
) -> None:
    body = {
        "error": {
            "message": f"Authentication Fails, Your api key: {shown} is invalid",
            "type": "authentication_error",
            "param": None,
            "code": "invalid_request_error",
        }
    }

    with pytest.raises(LlmError) as raised:
        await complete_with(answering(body, status), replace(ENDPOINT, api_key=ECHOED_KEY))

    logged = "".join(traceback.format_exception(raised.value))
    assert f"Authentication Fails, Your api key: {REDACTED} is invalid" in logged
    assert ECHOED_KEY[3:7] not in logged
    assert ECHOED_KEY[-4:] not in logged


@pytest.mark.parametrize(
    "error",
    [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow"), httpx2.RemoteProtocolError("closed")],
    ids=["connect error", "read timeout", "connection dropped"],
)
async def test_network_failure_is_reported_as_unavailable(error: Exception) -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(raising(error))


MALFORMED_BODIES: dict[str, Handler] = {
    "not json": lambda _: httpx2.Response(200, content=b"<html>gateway</html>"),
    "json without choices": answering({"id": "x"}),
    "empty choices": answering({"choices": []}),
    "choice without a message": answering({"choices": [{"finish_reason": "stop"}]}),
    "content that is not a string": answering(completion(content=5)),
    "finish reason that is not a string": answering(completion(finish_reason=1)),
    "json array": answering([completion()]),
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
        return httpx2.Response(200, json=completion())


async def test_cancelling_the_caller_aborts_the_request_in_flight() -> None:
    provider = SlowProvider()
    client = DeepSeekLlmClient(ENDPOINT, httpx2.MockTransport(provider.handle))
    try:
        call = asyncio.create_task(client.complete(PROMPT))
        await provider.reached.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        await client.aclose()

    assert provider.endings == ["aborted"]
