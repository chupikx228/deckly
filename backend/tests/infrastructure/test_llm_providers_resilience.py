from collections.abc import Callable
from dataclasses import dataclass

import httpx2
import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.infrastructure.llm.anthropic_client import AnthropicLlmClient
from deckly.infrastructure.llm.client import (
    LlmClient,
    LlmEndpoint,
    LlmPrompt,
    LlmRejectedError,
    LlmResponseError,
    LlmStop,
)
from deckly.infrastructure.llm.deepseek_client import DeepSeekLlmClient
from deckly.infrastructure.llm.gemini_client import GeminiLlmClient
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.resilience import CircuitBreaker, CircuitState, ResilientCaller, RetryPolicy
from tests.fakes import ManualTime, fresh_probe

pytestmark = pytest.mark.anyio

ENDPOINT = LlmEndpoint(
    base_url="https://provider.test",
    api_key="provider-test-key",
    model="model-test",
    max_output_tokens=1000,
    timeout_seconds=5,
)
PROMPT = LlmPrompt(system="system", user="user", expected_output_tokens=100)
MAX_ATTEMPTS = 3
POLICY = RetryPolicy(
    max_attempts=MAX_ATTEMPTS,
    attempt_timeout_seconds=10,
    deadline_seconds=100,
    base_delay_seconds=1,
    max_delay_seconds=8,
)
REPLY_TEXT = '{"verdict": "allow"}'
DEEPLY_NESTED = b"[" * 100_000 + b"]" * 100_000


@dataclass(frozen=True, slots=True)
class Provider:
    build: Callable[[httpx2.MockTransport], LlmClient]
    reply: dict[str, object]
    refusal: dict[str, object]


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        build=lambda transport: AnthropicLlmClient(ENDPOINT, httpx2.AsyncClient(transport=transport)),
        reply={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "model-test",
            "content": [{"type": "text", "text": REPLY_TEXT}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        refusal={
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "model": "model-test",
            "content": [],
            "stop_reason": "refusal",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        },
    ),
    "deepseek": Provider(
        build=lambda transport: DeepSeekLlmClient(ENDPOINT, transport),
        reply={"choices": [{"finish_reason": "stop", "message": {"content": REPLY_TEXT}}]},
        refusal={"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]},
    ),
    "gemini": Provider(
        build=lambda transport: GeminiLlmClient(ENDPOINT, transport),
        reply={"candidates": [{"content": {"parts": [{"text": REPLY_TEXT}]}, "finishReason": "STOP"}]},
        refusal={"promptFeedback": {"blockReason": "SAFETY"}},
    ),
}

ERROR_BODY: dict[str, object] = {"error": {"message": "failure"}}


class ScriptedProvider:
    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.calls = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        del request
        self.calls += 1
        return self.responses.pop(0)


def failing(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, json=ERROR_BODY, headers=headers)


def resilient(
    provider: Provider, scripted: ScriptedProvider, time: ManualTime, breaker: CircuitBreaker
) -> ResilientLlmClient:
    caller = ResilientCaller(POLICY, breaker, time.runtime(), probe=fresh_probe())
    inner = provider.build(httpx2.MockTransport(scripted.handle))
    return ResilientLlmClient(inner, caller, ENDPOINT.max_output_tokens)


parametrized = pytest.mark.parametrize("provider", PROVIDERS.values(), ids=PROVIDERS.keys())


@parametrized
async def test_transient_failure_is_retried_until_the_provider_answers(provider: Provider) -> None:
    time = ManualTime()
    scripted = ScriptedProvider(failing(503), failing(429), httpx2.Response(200, json=provider.reply))
    client = resilient(provider, scripted, time, time.breaker())
    try:
        reply = await client.complete(PROMPT)
    finally:
        await client.aclose()

    assert (reply.text, reply.stop) == (REPLY_TEXT, LlmStop.COMPLETE)
    assert scripted.calls == MAX_ATTEMPTS
    assert len(time.sleeps) == MAX_ATTEMPTS - 1


@parametrized
async def test_retry_after_from_the_provider_is_the_minimum_delay_before_the_retry(
    provider: Provider,
) -> None:
    time = ManualTime()
    scripted = ScriptedProvider(failing(429, {"retry-after": "6"}), httpx2.Response(200, json=provider.reply))
    client = resilient(provider, scripted, time, time.breaker())
    try:
        await client.complete(PROMPT)
    finally:
        await client.aclose()

    [delay] = time.sleeps
    assert delay >= 6


@parametrized
async def test_rejected_request_fails_fast_without_retrying_or_counting_against_the_circuit(
    provider: Provider,
) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    scripted = ScriptedProvider(failing(400))
    client = resilient(provider, scripted, time, breaker)
    try:
        with pytest.raises(LlmRejectedError):
            await client.complete(PROMPT)
    finally:
        await client.aclose()

    assert scripted.calls == 1
    assert breaker.state is CircuitState.CLOSED


@parametrized
async def test_refusal_is_an_answer_that_is_neither_retried_nor_counted_as_a_failure(
    provider: Provider,
) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    scripted = ScriptedProvider(httpx2.Response(200, json=provider.refusal))
    client = resilient(provider, scripted, time, breaker)
    try:
        reply = await client.complete(PROMPT)
    finally:
        await client.aclose()

    assert reply.stop is LlmStop.REFUSED
    assert scripted.calls == 1
    assert breaker.state is CircuitState.CLOSED


@parametrized
async def test_exhausted_retries_raise_upstream_unavailable_and_open_the_circuit(provider: Provider) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=MAX_ATTEMPTS)
    scripted = ScriptedProvider(*(failing(503) for _ in range(MAX_ATTEMPTS)))
    client = resilient(provider, scripted, time, breaker)
    try:
        with pytest.raises(UpstreamUnavailableError):
            await client.complete(PROMPT)
    finally:
        await client.aclose()

    assert scripted.calls == MAX_ATTEMPTS
    assert breaker.state is CircuitState.OPEN


@parametrized
async def test_answer_nested_too_deeply_to_parse_is_a_response_error_not_a_crash(provider: Provider) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    scripted = ScriptedProvider(
        httpx2.Response(200, content=DEEPLY_NESTED, headers={"content-type": "application/json"})
    )
    client = resilient(provider, scripted, time, breaker)
    try:
        with pytest.raises(LlmResponseError):
            await client.complete(PROMPT)
    finally:
        await client.aclose()

    assert scripted.calls == 1
    assert breaker.state is CircuitState.CLOSED
