import asyncio
import logging
import time

import pytest

from deckly.application.exceptions import NoValidContentError, RateLimitedError, UpstreamUnavailableError
from deckly.application.regeneration import TIMED_OUT_RETRY_AFTER_SECONDS
from deckly.domain.generation import Difficulty
from deckly.domain.notes.note_type import NoteType
from tests.fakes import (
    MATERIAL,
    PAGES,
    RegenerationHarness,
    RegenerationStep,
    job_id,
    regeneration_request,
)

pytestmark = pytest.mark.anyio

CLIENT_ID = job_id(42)
GUARD_SECONDS = 0.05
PROMPT_RETURN_SECONDS = 1.0
REGENERATION_LOGGER = "deckly.application.regeneration"


async def hang() -> None:
    await asyncio.Event().wait()


async def test_note_is_regenerated_from_freshly_searched_material() -> None:
    harness = RegenerationHarness()
    request = regeneration_request(NoteType.CLOZE)

    note = await harness.regenerate(request, CLIENT_ID)

    providers = harness.providers
    assert note is providers.note
    assert providers.steps == list(RegenerationStep)
    assert providers.acquired == [(CLIENT_ID, harness.now)]
    request_id, search = providers.searched[0]
    assert (search.topic, search.language, search.note_types) == ("Road signs", "ru", (NoteType.CLOZE,))
    assert (search.card_count, search.difficulty, search.include_images) == (
        1,
        Difficulty.INTERMEDIATE,
        False,
    )
    assert providers.parsed == [(request_id, PAGES)]
    assert providers.regenerated == [(request_id, request, MATERIAL)]


async def test_each_call_gets_its_own_request_id() -> None:
    harness = RegenerationHarness()

    await harness.regenerate(regeneration_request(), CLIENT_ID)
    await harness.regenerate(regeneration_request(), CLIENT_ID)

    assert [request_id for request_id, _ in harness.providers.searched] == [job_id(1), job_id(2)]


async def test_rate_limited_client_is_refused_before_any_provider_is_called() -> None:
    harness = RegenerationHarness()
    harness.providers.failures[RegenerationStep.LIMIT] = RateLimitedError(120)

    with pytest.raises(RateLimitedError) as raised:
        await harness.regenerate(regeneration_request(), CLIENT_ID)

    assert raised.value.retry_after_seconds == 120
    assert harness.providers.steps == [RegenerationStep.LIMIT]


@pytest.mark.parametrize("step", list(RegenerationStep))
async def test_upstream_outage_at_any_step_is_reported_as_unavailable(step: RegenerationStep) -> None:
    harness = RegenerationHarness()
    harness.providers.failures[step] = UpstreamUnavailableError(3)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await harness.regenerate(regeneration_request(), CLIENT_ID)

    assert raised.value.retry_after_seconds == 3


async def test_no_valid_note_is_reported_as_such() -> None:
    harness = RegenerationHarness()
    harness.providers.failures[RegenerationStep.GENERATE] = NoValidContentError("all dropped")

    with pytest.raises(NoValidContentError):
        await harness.regenerate(regeneration_request(), CLIENT_ID)


@pytest.mark.parametrize("step", list(RegenerationStep))
async def test_step_that_hangs_is_cut_off_by_the_overall_guard_and_cancelled(
    step: RegenerationStep, caplog: pytest.LogCaptureFixture
) -> None:
    harness = RegenerationHarness(timeout_seconds=GUARD_SECONDS)
    harness.providers.during[step] = hang
    started = time.monotonic()

    with (
        caplog.at_level(logging.WARNING, logger=REGENERATION_LOGGER),
        pytest.raises(UpstreamUnavailableError) as raised,
    ):
        await harness.regenerate(regeneration_request(), CLIENT_ID)

    assert time.monotonic() - started < PROMPT_RETURN_SECONDS
    assert raised.value.retry_after_seconds == TIMED_OUT_RETRY_AFTER_SECONDS
    assert harness.providers.cancelled == [step]
    assert harness.providers.steps[-1] is step
    assert [record.message for record in caplog.records] == ["note_regeneration_timed_out"]


async def test_timeout_raised_inside_a_step_is_reported_as_unavailable() -> None:
    harness = RegenerationHarness()
    harness.providers.failures[RegenerationStep.SEARCH] = TimeoutError()

    with pytest.raises(UpstreamUnavailableError):
        await harness.regenerate(regeneration_request(), CLIENT_ID)


async def test_unexpected_failure_is_not_disguised() -> None:
    harness = RegenerationHarness()
    harness.providers.failures[RegenerationStep.PARSE] = RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        await harness.regenerate(regeneration_request(), CLIENT_ID)


async def test_caller_cancellation_is_not_turned_into_an_error() -> None:
    harness = RegenerationHarness()
    reached = asyncio.Event()

    async def hang_once_reached() -> None:
        reached.set()
        await hang()

    harness.providers.during[RegenerationStep.GENERATE] = hang_once_reached
    task = asyncio.create_task(harness.regenerate(regeneration_request(), CLIENT_ID))
    await reached.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.providers.cancelled == [RegenerationStep.GENERATE]
