import asyncio
import logging
from dataclasses import replace

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.infrastructure.llm.client import (
    LlmPrompt,
    LlmRejectedError,
    LlmReply,
    LlmResponseError,
    LlmStop,
    LlmUnavailableError,
)
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.resilience import (
    CallSize,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ResilientCaller,
    RetryPolicy,
    RetryRuntime,
)
from tests.fakes import RESET_SECONDS, FakeLlmClient, ManualTime, hang_forever

pytestmark = pytest.mark.anyio

MAX_OUTPUT_TOKENS = 1000
PROMPT = LlmPrompt(system="system", user="user", expected_output_tokens=100)
REPLY = LlmReply(text="{}", stop=LlmStop.COMPLETE)
POLICY = RetryPolicy(
    max_attempts=3,
    attempt_timeout_seconds=10,
    deadline_seconds=100,
    base_delay_seconds=1,
    max_delay_seconds=8,
)


def resilient(
    inner: FakeLlmClient,
    time: ManualTime,
    policy: RetryPolicy = POLICY,
    breaker: CircuitBreaker | None = None,
) -> ResilientLlmClient:
    runtime = RetryRuntime(clock=time.clock, sleep=time.sleep, jitter=time.jitter)
    return ResilientLlmClient(
        inner, ResilientCaller(policy, breaker or time.breaker(), runtime), MAX_OUTPUT_TOKENS
    )


def unavailable(retry_after_seconds: float | None = None) -> LlmUnavailableError:
    return LlmUnavailableError("provider answered 503", retry_after_seconds=retry_after_seconds)


async def test_reply_on_the_first_attempt_is_returned_without_waiting() -> None:
    time = ManualTime()
    inner = FakeLlmClient(REPLY)

    assert await resilient(inner, time).complete(PROMPT) == REPLY
    assert inner.prompts == [PROMPT]
    assert time.sleeps == []


async def test_transient_failures_are_retried_with_exponential_backoff_until_one_succeeds() -> None:
    time = ManualTime()
    inner = FakeLlmClient(unavailable(), unavailable(), REPLY)

    assert await resilient(inner, time).complete(PROMPT) == REPLY
    assert len(inner.prompts) == 3
    assert time.sleeps == [1.0, 2.0]


async def test_backoff_is_jittered_between_zero_and_the_exponential_ceiling() -> None:
    time = ManualTime()
    time.fraction = 0.25
    inner = FakeLlmClient(unavailable(), unavailable(), REPLY)

    await resilient(inner, time).complete(PROMPT)

    assert time.sleeps == [0.25, 0.5]


async def test_backoff_never_exceeds_the_maximum_delay() -> None:
    time = ManualTime()
    inner = FakeLlmClient(*[unavailable()] * 5, REPLY)
    breaker = time.breaker(failure_threshold=10)

    await resilient(inner, time, replace(POLICY, max_attempts=6), breaker).complete(PROMPT)

    assert time.sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_backoff_ceiling_does_not_overflow_for_an_absurd_attempt_number() -> None:
    assert POLICY.backoff_ceiling(1_000_000) == POLICY.max_delay_seconds


async def test_exhausted_retries_raise_upstream_unavailable_chained_to_the_last_failure() -> None:
    time = ManualTime()
    last = unavailable()
    inner = FakeLlmClient(unavailable(), unavailable(), last)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await resilient(inner, time).complete(PROMPT)

    assert len(inner.prompts) == POLICY.max_attempts
    assert raised.value.__cause__ is last
    assert raised.value.retry_after_seconds >= 1


async def test_provider_call_that_times_out_is_retried_and_then_raises_upstream_unavailable() -> None:
    time = ManualTime()
    inner = FakeLlmClient(hang_forever)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await resilient(inner, time, replace(POLICY, attempt_timeout_seconds=0.01)).complete(PROMPT)

    assert len(inner.prompts) == POLICY.max_attempts
    assert isinstance(raised.value.__cause__, TimeoutError)


async def test_retry_after_requested_by_the_provider_is_the_minimum_delay() -> None:
    time = ManualTime()
    inner = FakeLlmClient(unavailable(retry_after_seconds=5), REPLY)

    await resilient(inner, time).complete(PROMPT)

    assert time.sleeps == [5.0]


async def test_retry_that_could_not_finish_before_the_deadline_is_not_started() -> None:
    time = ManualTime()

    async def slow_failure() -> LlmReply:
        time.now += 9
        raise unavailable()

    inner = FakeLlmClient(slow_failure)

    with pytest.raises(UpstreamUnavailableError):
        await resilient(inner, time, replace(POLICY, deadline_seconds=15)).complete(PROMPT)

    assert len(inner.prompts) == 1
    assert time.sleeps == []


async def test_retry_that_would_finish_exactly_at_the_deadline_is_still_attempted() -> None:
    time = ManualTime()

    async def slow_failure() -> LlmReply:
        time.now += 9
        raise unavailable()

    inner = FakeLlmClient(slow_failure, REPLY)

    reply = await resilient(inner, time, replace(POLICY, deadline_seconds=20)).complete(PROMPT)

    assert reply == REPLY
    assert len(inner.prompts) == 2


async def test_retry_after_longer_than_the_remaining_deadline_gives_up_with_that_hint() -> None:
    time = ManualTime()
    inner = FakeLlmClient(unavailable(retry_after_seconds=500))

    with pytest.raises(UpstreamUnavailableError) as raised:
        await resilient(inner, time).complete(PROMPT)

    assert len(inner.prompts) == 1
    assert raised.value.retry_after_seconds == 500


@pytest.mark.parametrize("error", [LlmRejectedError("401"), LlmResponseError("no choices")])
async def test_non_transient_failure_is_raised_as_is_without_retrying_or_tripping_the_circuit(
    error: Exception,
) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    inner = FakeLlmClient(error)

    with pytest.raises(type(error)) as raised:
        await resilient(inner, time, breaker=breaker).complete(PROMPT)

    assert not isinstance(raised.value, UpstreamUnavailableError)
    assert len(inner.prompts) == 1
    assert breaker.state is CircuitState.CLOSED


async def test_consecutive_transient_failures_open_the_circuit_and_later_calls_fail_fast() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=3)
    inner = FakeLlmClient(unavailable())
    client = resilient(inner, time, replace(POLICY, max_attempts=1), breaker)
    for _ in range(3):
        with pytest.raises(UpstreamUnavailableError):
            await client.complete(PROMPT)

    with pytest.raises(CircuitOpenError) as raised:
        await client.complete(PROMPT)

    assert breaker.state is CircuitState.OPEN
    assert len(inner.prompts) == 3
    assert isinstance(raised.value, UpstreamUnavailableError)
    assert raised.value.retry_after_seconds == RESET_SECONDS


async def test_open_circuit_stops_the_retries_of_a_call_already_in_progress() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=2)
    inner = FakeLlmClient(unavailable())

    with pytest.raises(CircuitOpenError):
        await resilient(inner, time, replace(POLICY, max_attempts=5), breaker).complete(PROMPT)

    assert len(inner.prompts) == 2


async def test_success_resets_the_count_of_consecutive_failures() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=3)
    inner = FakeLlmClient(unavailable(), unavailable(), REPLY, unavailable(), unavailable(), REPLY)
    client = resilient(inner, time, replace(POLICY, max_attempts=3), breaker)

    await client.complete(PROMPT)
    await client.complete(PROMPT)

    assert breaker.state is CircuitState.CLOSED


async def open_circuit(time: ManualTime, breaker: CircuitBreaker) -> None:
    client = resilient(FakeLlmClient(unavailable()), time, replace(POLICY, max_attempts=1), breaker)
    with pytest.raises(UpstreamUnavailableError):
        await client.complete(PROMPT)
    assert breaker.state is CircuitState.OPEN


async def test_open_circuit_lets_one_trial_through_after_the_reset_time_and_closes_on_success() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    await open_circuit(time, breaker)
    time.now += RESET_SECONDS
    inner = FakeLlmClient(REPLY)

    assert await resilient(inner, time, breaker=breaker).complete(PROMPT) == REPLY
    assert breaker.state is CircuitState.CLOSED
    assert len(inner.prompts) == 1


async def test_failed_trial_reopens_the_circuit_for_another_reset_period() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    await open_circuit(time, breaker)
    time.now += RESET_SECONDS
    inner = FakeLlmClient(unavailable())
    client = resilient(inner, time, replace(POLICY, max_attempts=1), breaker)

    with pytest.raises(UpstreamUnavailableError):
        await client.complete(PROMPT)
    with pytest.raises(CircuitOpenError):
        await client.complete(PROMPT)

    assert breaker.state is CircuitState.OPEN
    assert len(inner.prompts) == 1


async def test_only_one_trial_runs_while_the_circuit_is_half_open() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    await open_circuit(time, breaker)
    time.now += RESET_SECONDS
    reached, release = asyncio.Event(), asyncio.Event()

    async def slow_success() -> LlmReply:
        reached.set()
        await release.wait()
        return REPLY

    inner = FakeLlmClient(slow_success)
    client = resilient(inner, time, breaker=breaker)
    trial = asyncio.create_task(client.complete(PROMPT))
    await reached.wait()

    with pytest.raises(CircuitOpenError):
        await client.complete(PROMPT)
    release.set()

    assert await trial == REPLY
    assert breaker.state is CircuitState.CLOSED
    assert len(inner.prompts) == 1


async def test_cancelled_trial_releases_the_circuit_so_the_next_call_is_tried() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    await open_circuit(time, breaker)
    time.now += RESET_SECONDS
    reached = asyncio.Event()

    async def hang_after_reaching() -> LlmReply:
        reached.set()
        return await hang_forever()

    inner = FakeLlmClient(hang_after_reaching, REPLY)
    client = resilient(inner, time, breaker=breaker)
    trial = asyncio.create_task(client.complete(PROMPT))
    await reached.wait()
    trial.cancel()

    with pytest.raises(asyncio.CancelledError):
        await trial
    released = breaker.state
    reply = await client.complete(PROMPT)

    assert (released, reply, breaker.state) == (CircuitState.OPEN, REPLY, CircuitState.CLOSED)
    assert time.sleeps == []


async def test_cancelling_the_caller_is_propagated_instead_of_being_retried() -> None:
    time = ManualTime()
    reached = asyncio.Event()

    async def hang_after_reaching() -> LlmReply:
        reached.set()
        return await hang_forever()

    inner = FakeLlmClient(hang_after_reaching)
    call = asyncio.create_task(resilient(inner, time).complete(PROMPT))
    await reached.wait()
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call

    assert len(inner.prompts) == 1
    assert time.sleeps == []


async def test_cancelled_call_is_not_counted_against_the_provider() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    reached = asyncio.Event()

    async def hang_after_reaching() -> LlmReply:
        reached.set()
        return await hang_forever()

    client = resilient(FakeLlmClient(hang_after_reaching, REPLY), time, breaker=breaker)
    call = asyncio.create_task(client.complete(PROMPT))
    await reached.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert breaker.state is CircuitState.CLOSED
    assert await client.complete(PROMPT) == REPLY


async def test_cancelling_during_the_backoff_starts_no_further_attempt() -> None:
    time = ManualTime()
    backing_off = asyncio.Event()

    async def sleep_until_cancelled(seconds: float) -> None:
        time.sleeps.append(seconds)
        backing_off.set()
        await asyncio.Event().wait()

    inner = FakeLlmClient(unavailable(), REPLY)
    runtime = RetryRuntime(clock=time.clock, sleep=sleep_until_cancelled, jitter=time.jitter)
    client = ResilientLlmClient(inner, ResilientCaller(POLICY, time.breaker(), runtime), MAX_OUTPUT_TOKENS)
    call = asyncio.create_task(client.complete(PROMPT))
    await backing_off.wait()
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call

    assert len(inner.prompts) == 1


TYPICAL_SIZES = {"small": 100, "just under half the budget": MAX_OUTPUT_TOKENS // 2 - 1}
OVERSIZED_SIZES = {
    "exactly half the budget": MAX_OUTPUT_TOKENS // 2,
    "the whole budget": MAX_OUTPUT_TOKENS,
    "beyond the budget": MAX_OUTPUT_TOKENS * 5,
}
IMPATIENT_POLICY = replace(POLICY, max_attempts=1, attempt_timeout_seconds=0.01)


def expecting(tokens: int) -> LlmPrompt:
    return replace(PROMPT, expected_output_tokens=tokens)


@pytest.mark.parametrize("tokens", OVERSIZED_SIZES.values(), ids=OVERSIZED_SIZES.keys())
async def test_timeouts_of_oversized_requests_never_open_the_circuit(tokens: int) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=2)
    inner = FakeLlmClient(hang_forever)
    client = resilient(inner, time, IMPATIENT_POLICY, breaker)

    for _ in range(5):
        with pytest.raises(UpstreamUnavailableError):
            await client.complete(expecting(tokens))

    assert breaker.state is CircuitState.CLOSED
    assert len(inner.prompts) == 5


@pytest.mark.parametrize("tokens", TYPICAL_SIZES.values(), ids=TYPICAL_SIZES.keys())
async def test_timeouts_of_typical_requests_still_open_the_circuit(tokens: int) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=2)
    inner = FakeLlmClient(hang_forever)
    client = resilient(inner, time, IMPATIENT_POLICY, breaker)
    for _ in range(2):
        with pytest.raises(UpstreamUnavailableError):
            await client.complete(expecting(tokens))

    with pytest.raises(CircuitOpenError):
        await client.complete(expecting(tokens))

    assert breaker.state is CircuitState.OPEN
    assert len(inner.prompts) == 2


async def test_failures_other_than_timeouts_open_the_circuit_whatever_the_request_size() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=2)
    inner = FakeLlmClient(unavailable())
    client = resilient(inner, time, replace(POLICY, max_attempts=1), breaker)
    for _ in range(2):
        with pytest.raises(UpstreamUnavailableError):
            await client.complete(expecting(MAX_OUTPUT_TOKENS))

    with pytest.raises(CircuitOpenError):
        await client.complete(expecting(MAX_OUTPUT_TOKENS))

    assert len(inner.prompts) == 2


async def test_oversized_timeout_neither_counts_nor_resets_the_consecutive_failures() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=2)
    inner = FakeLlmClient(hang_forever)
    client = resilient(inner, time, IMPATIENT_POLICY, breaker)

    for tokens in (PROMPT.expected_output_tokens, MAX_OUTPUT_TOKENS, PROMPT.expected_output_tokens):
        with pytest.raises(UpstreamUnavailableError):
            await client.complete(expecting(tokens))

    assert breaker.state is CircuitState.OPEN
    assert len(inner.prompts) == 3


async def test_oversized_trial_that_times_out_lets_the_next_call_try_again_at_once() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    await open_circuit(time, breaker)
    time.now += RESET_SECONDS
    client = resilient(FakeLlmClient(hang_forever, REPLY), time, IMPATIENT_POLICY, breaker)

    with pytest.raises(UpstreamUnavailableError):
        await client.complete(expecting(MAX_OUTPUT_TOKENS))
    released = breaker.state
    reply = await client.complete(PROMPT)

    assert (released, reply, breaker.state) == (CircuitState.OPEN, REPLY, CircuitState.CLOSED)


async def test_typical_trial_that_times_out_reopens_the_circuit_for_another_reset_period() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    await open_circuit(time, breaker)
    time.now += RESET_SECONDS
    inner = FakeLlmClient(hang_forever, REPLY)
    client = resilient(inner, time, IMPATIENT_POLICY, breaker)

    with pytest.raises(UpstreamUnavailableError):
        await client.complete(PROMPT)
    with pytest.raises(CircuitOpenError):
        await client.complete(PROMPT)

    assert len(inner.prompts) == 1


async def test_retry_is_logged_with_the_size_of_the_call(caplog: pytest.LogCaptureFixture) -> None:
    inner = FakeLlmClient(unavailable(), REPLY)

    with caplog.at_level(logging.WARNING, logger="deckly.infrastructure.resilience"):
        await resilient(inner, ManualTime()).complete(expecting(MAX_OUTPUT_TOKENS))

    [record] = [record for record in caplog.records if record.getMessage() == "provider_call_retrying"]
    assert record.__dict__["call_size"] == CallSize.OVERSIZED


async def test_closing_the_resilient_client_closes_the_provider_client() -> None:
    inner = FakeLlmClient(REPLY)

    await resilient(inner, ManualTime()).aclose()

    assert inner.closed
