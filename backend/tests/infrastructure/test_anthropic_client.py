import json
from collections.abc import Callable

import httpx2
import pytest

from deckly.infrastructure.llm.anthropic_client import AnthropicLlmClient
from deckly.infrastructure.llm.client import (
    LlmEndpoint,
    LlmPrompt,
    LlmRejectedError,
    LlmReply,
    LlmStop,
    LlmUnavailableError,
)
from deckly.infrastructure.resilience import TransientError

pytestmark = pytest.mark.anyio

API_KEY = "test-anthropic-key"
ENDPOINT = LlmEndpoint(
    base_url="https://api.anthropic.test",
    api_key=API_KEY,
    model="claude-test",
    max_output_tokens=1234,
    timeout_seconds=5,
)
PROMPT = LlmPrompt(system="system instructions", user="user request")

type Handler = Callable[[httpx2.Request], httpx2.Response]


def message(
    content: list[dict[str, object]] | None = None, stop_reason: str | None = "end_turn"
) -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "{}"}] if content is None else content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def failure(status: int, headers: dict[str, str] | None = None) -> Handler:
    body = {"type": "error", "error": {"type": "api_error", "message": f"status {status}"}}
    return lambda _: httpx2.Response(status, json=body, headers=headers)


def raising(error: Exception) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        raise error

    return handler


async def complete_with(handler: Handler) -> LlmReply:
    client = AnthropicLlmClient(ENDPOINT, httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    try:
        return await client.complete(PROMPT)
    finally:
        await client.aclose()


async def test_request_carries_the_prompt_model_and_token_cap_and_nothing_else() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=message())

    await complete_with(handler)

    [request] = seen
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == API_KEY
    assert json.loads(request.content) == {
        "model": "claude-test",
        "max_tokens": 1234,
        "system": "system instructions",
        "messages": [{"role": "user", "content": "user request"}],
    }


async def test_text_blocks_are_joined_and_other_blocks_ignored() -> None:
    content: list[dict[str, object]] = [
        {"type": "thinking", "thinking": "planning the deck", "signature": "sig"},
        {"type": "text", "text": '{"deck": '},
        {"type": "text", "text": "{}}"},
    ]

    reply = await complete_with(lambda _: httpx2.Response(200, json=message(content)))

    assert reply == LlmReply(text='{"deck": {}}', stop=LlmStop.COMPLETE)


STOP_REASONS = {
    "end_turn": LlmStop.COMPLETE,
    "stop_sequence": LlmStop.COMPLETE,
    "max_tokens": LlmStop.TRUNCATED,
    "model_context_window_exceeded": LlmStop.TRUNCATED,
    "refusal": LlmStop.REFUSED,
    "a_reason_added_later": LlmStop.COMPLETE,
}


@pytest.mark.parametrize(("stop_reason", "stop"), STOP_REASONS.items(), ids=STOP_REASONS.keys())
async def test_stop_reason_maps_to_how_the_reply_ended(stop_reason: str, stop: LlmStop) -> None:
    reply = await complete_with(lambda _: httpx2.Response(200, json=message(stop_reason=stop_reason)))

    assert reply.stop is stop


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 529])
async def test_transient_status_is_reported_as_unavailable(status: int) -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(failure(status))


async def test_retry_after_header_is_passed_on_with_the_transient_failure() -> None:
    with pytest.raises(LlmUnavailableError) as raised:
        await complete_with(failure(429, {"retry-after": "7"}))

    assert raised.value.retry_after_seconds == 7.0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
async def test_rejected_request_is_not_reported_as_transient(status: int) -> None:
    with pytest.raises(LlmRejectedError) as raised:
        await complete_with(failure(status))

    assert not isinstance(raised.value, TransientError)
    assert API_KEY not in str(raised.value)


@pytest.mark.parametrize(
    "error",
    [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow"), httpx2.RemoteProtocolError("closed")],
    ids=["connect error", "read timeout", "connection dropped"],
)
async def test_network_failure_is_reported_as_unavailable(error: Exception) -> None:
    with pytest.raises(LlmUnavailableError):
        await complete_with(raising(error))
