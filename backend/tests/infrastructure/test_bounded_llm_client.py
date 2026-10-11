import asyncio
import json
import math

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.infrastructure.llm.bounded import BoundedLlmClient
from deckly.infrastructure.llm.client import LlmClient, LlmPrompt, LlmReply, LlmStop, LlmUnavailableError
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.moderation.moderator import NOTES_PER_CONTENT_CHECK, LlmContentModerator
from deckly.infrastructure.resilience import ResilientCaller, RetryPolicy, RetryRuntime
from deckly.transport.generations import MAX_CARD_COUNT
from tests.domain.builders import basic_note, result_with
from tests.fakes import (
    FakeLlmClient,
    ManualTime,
    fresh_probe,
    generation_request,
    hang_forever,
    job_id,
    model_reply,
)

pytestmark = pytest.mark.anyio

LIMIT = 10
FAIL_FAST_SECONDS = 1
SLICES_PER_CALL = 3
QUEUED_JOBS = 25
QUEUED_FAILING_JOBS = 3
CHUNKS_PER_MAX_DECK = math.ceil(MAX_CARD_COUNT / NOTES_PER_CONTENT_CHECK)
MODERATION_MAX_OUTPUT_TOKENS = 4000
REPLY = LlmReply(text="{}", stop=LlmStop.COMPLETE)
PROMPT = LlmPrompt(system="system", user=json.dumps({"notes": {"1": "front"}}), expected_output_tokens=100)
MAX_DECK = result_with(*(basic_note(number) for number in range(1, MAX_CARD_COUNT + 1)))
REQUEST = generation_request()


class CountingClassifier:
    def __init__(self, *, parties: int = 1) -> None:
        self.parties = parties
        self.arrived = asyncio.Event()
        self.calls = 0
        self.in_flight = 0
        self.peak = 0
        self.closed = False

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.in_flight >= self.parties:
                self.arrived.set()
            await self.arrived.wait()
            for _ in range(SLICES_PER_CALL):
                await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        notes: dict[str, object] = json.loads(prompt.user)["notes"]
        return model_reply({"deck": "allow", "notes": dict.fromkeys(notes, "allow")})

    async def aclose(self) -> None:
        self.closed = True


def bounded(inner: LlmClient, limit: int = LIMIT) -> BoundedLlmClient:
    return BoundedLlmClient(inner, asyncio.Semaphore(limit))


async def test_calls_in_flight_never_exceed_the_limit_and_every_queued_call_is_still_answered() -> None:
    provider = CountingClassifier()
    client = bounded(provider, limit=3)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        replies = await asyncio.gather(*(client.complete(PROMPT) for _ in range(30)))

    assert provider.peak == 3
    assert provider.calls == 30
    assert provider.in_flight == 0
    assert all(reply.stop is LlmStop.COMPLETE for reply in replies)


async def test_calls_within_the_limit_all_run_together_instead_of_queueing() -> None:
    provider = CountingClassifier(parties=4)
    client = bounded(provider)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        await asyncio.gather(*(client.complete(PROMPT) for _ in range(4)))

    assert provider.peak == 4


async def test_clients_sharing_a_gate_share_its_limit() -> None:
    provider = CountingClassifier()
    gate = asyncio.Semaphore(4)
    moderation = BoundedLlmClient(provider, gate)
    images = BoundedLlmClient(provider, gate)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        await asyncio.gather(
            *(moderation.complete(PROMPT) for _ in range(10)), *(images.complete(PROMPT) for _ in range(10))
        )

    assert provider.peak == 4
    assert provider.calls == 20


async def test_failed_call_gives_its_slot_back() -> None:
    client = bounded(FakeLlmClient(LlmUnavailableError("down"), REPLY), limit=1)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        outcomes = await asyncio.gather(
            client.complete(PROMPT), client.complete(PROMPT), return_exceptions=True
        )

    assert [type(outcome) for outcome in outcomes] == [LlmUnavailableError, LlmReply]


async def test_cancelled_call_gives_its_slot_to_the_next_queued_call() -> None:
    client = bounded(FakeLlmClient(hang_forever, REPLY), limit=1)
    holder = asyncio.create_task(client.complete(PROMPT))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(client.complete(PROMPT))
    await asyncio.sleep(0)
    assert not waiter.done()

    holder.cancel()

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        assert await waiter == REPLY


async def test_closing_closes_the_wrapped_client() -> None:
    provider = CountingClassifier()

    await bounded(provider).aclose()

    assert provider.closed


QUEUE_SLOT_SECONDS = 0.15
ATTEMPT_TIMEOUT_SECONDS = 0.4
QUEUED_CALLS = 4


class SlowClassifier(CountingClassifier):
    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        await asyncio.sleep(QUEUE_SLOT_SECONDS)
        return await super().complete(prompt)


def single_attempt(inner: LlmClient, timeout_seconds: float) -> ResilientLlmClient:
    policy = RetryPolicy(
        max_attempts=1,
        attempt_timeout_seconds=timeout_seconds,
        deadline_seconds=timeout_seconds,
        base_delay_seconds=1,
        max_delay_seconds=1,
    )
    time = ManualTime()
    runtime = RetryRuntime(clock=time.clock, sleep=time.sleep, jitter=time.jitter)
    caller = ResilientCaller(policy, time.breaker(), runtime, probe=fresh_probe())
    return ResilientLlmClient(inner, caller, MODERATION_MAX_OUTPUT_TOKENS)


async def test_time_spent_queued_is_not_charged_to_the_attempt_timeout_of_the_resilience_layer() -> None:
    provider = SlowClassifier()
    client = bounded(single_attempt(provider, ATTEMPT_TIMEOUT_SECONDS), limit=1)

    replies = await asyncio.gather(*(client.complete(PROMPT) for _ in range(QUEUED_CALLS)))

    assert QUEUED_CALLS * QUEUE_SLOT_SECONDS > ATTEMPT_TIMEOUT_SECONDS
    assert [reply.stop for reply in replies] == [LlmStop.COMPLETE] * QUEUED_CALLS
    assert provider.peak == 1


async def screen_queued_decks(moderator: LlmContentModerator) -> tuple[int, ...]:
    async with asyncio.timeout(FAIL_FAST_SECONDS):
        screened = await asyncio.gather(
            *(moderator.screen(job_id(number), REQUEST, MAX_DECK) for number in range(QUEUED_JOBS))
        )
    return tuple(len(result.notes) for result in screened)


async def test_burst_of_maximum_size_decks_never_puts_more_than_the_limit_of_calls_in_flight() -> None:
    provider = CountingClassifier()
    moderator = LlmContentModerator(llm=bounded(provider))

    kept = await screen_queued_decks(moderator)

    assert provider.calls == QUEUED_JOBS * CHUNKS_PER_MAX_DECK
    assert provider.peak == LIMIT
    assert kept == (MAX_CARD_COUNT,) * QUEUED_JOBS


async def test_the_same_burst_without_the_gate_puts_every_chunk_in_flight_at_once() -> None:
    provider = CountingClassifier()

    await screen_queued_decks(LlmContentModerator(llm=provider))

    assert provider.peak == QUEUED_JOBS * CHUNKS_PER_MAX_DECK


async def test_chunks_of_one_maximum_size_deck_still_run_together_under_the_limit() -> None:
    provider = CountingClassifier(parties=CHUNKS_PER_MAX_DECK)
    moderator = LlmContentModerator(llm=bounded(provider))

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        screened = await moderator.screen(job_id(1), REQUEST, MAX_DECK)

    assert provider.peak == CHUNKS_PER_MAX_DECK
    assert len(screened.notes) == MAX_CARD_COUNT


async def test_provider_outage_fails_every_queued_job_instead_of_hanging_them() -> None:
    llm = bounded(single_attempt(FakeLlmClient(LlmUnavailableError("down")), FAIL_FAST_SECONDS), limit=2)
    moderator = LlmContentModerator(llm=llm)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        outcomes = await asyncio.gather(
            *(moderator.screen(job_id(number), REQUEST, MAX_DECK) for number in range(QUEUED_FAILING_JOBS)),
            return_exceptions=True,
        )

    assert all(isinstance(outcome, UpstreamUnavailableError) for outcome in outcomes)
    assert len(outcomes) == QUEUED_FAILING_JOBS


async def holds_every_slot(gate: asyncio.Semaphore, limit: int) -> bool:
    for _ in range(limit - 1):
        await gate.acquire()
    if gate.locked():
        return False
    await gate.acquire()
    return gate.locked()


class Gated:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        del prompt
        self.started += 1
        await self.release.wait()
        return REPLY

    async def aclose(self) -> None:
        return None


async def test_cancelling_a_queued_call_neither_leaks_a_slot_nor_strands_the_calls_behind_it() -> None:
    inner = Gated()
    gate = asyncio.Semaphore(1)
    client = BoundedLlmClient(inner, gate)
    holder = asyncio.create_task(client.complete(PROMPT))
    await asyncio.sleep(0)
    first, second, third = (asyncio.create_task(client.complete(PROMPT)) for _ in range(3))
    await asyncio.sleep(0)

    second.cancel()
    inner.release.set()

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        replies = list(await asyncio.gather(holder, first, third))
    assert replies == [REPLY] * 3
    assert second.cancelled()
    assert inner.started == 3
    assert await holds_every_slot(gate, 1)


async def test_waiter_cancelled_in_the_instant_its_slot_is_handed_over_passes_the_slot_on() -> None:
    inner = Gated()
    gate = asyncio.Semaphore(1)
    client = BoundedLlmClient(inner, gate)
    holder = asyncio.create_task(client.complete(PROMPT))
    await asyncio.sleep(0)
    woken = asyncio.create_task(client.complete(PROMPT))
    behind = asyncio.create_task(client.complete(PROMPT))
    await asyncio.sleep(0)

    inner.release.set()
    await holder
    woken.cancel()

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        assert await behind == REPLY
    assert await holds_every_slot(gate, 1)


async def test_failing_chunk_under_a_single_slot_cancels_the_chunks_still_queued() -> None:
    provider = FakeLlmClient(LlmUnavailableError("down"), hang_forever)
    gate = asyncio.Semaphore(1)
    llm = BoundedLlmClient(single_attempt(provider, FAIL_FAST_SECONDS), gate)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        with pytest.raises(UpstreamUnavailableError):
            await LlmContentModerator(llm=llm).screen(job_id(1), REQUEST, MAX_DECK)

    assert len(provider.prompts) <= 2
    assert await holds_every_slot(gate, 1)


async def test_gate_is_whole_again_after_a_burst_of_large_decks() -> None:
    provider = CountingClassifier()
    gate = asyncio.Semaphore(LIMIT)

    await screen_queued_decks(LlmContentModerator(llm=BoundedLlmClient(provider, gate)))

    assert await holds_every_slot(gate, LIMIT)
