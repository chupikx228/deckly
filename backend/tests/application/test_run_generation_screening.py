from dataclasses import replace
from uuid import UUID

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import Failed, FailureCode, JobStage, Succeeded
from tests.domain.builders import basic_note, result_with
from tests.fakes import ADDRESS, Harness, generation_request, image_query, scope, with_images

pytestmark = pytest.mark.anyio

WITH_IMAGES = replace(generation_request(), include_images=True)
WITHOUT_IMAGES = replace(generation_request(), include_images=False)
NOTES = (basic_note(1), basic_note(2), basic_note(3), basic_note(4))
ADVERSARIAL = NOTES[2]


async def create(harness: Harness, request: GenerationRequest) -> UUID:
    return (await harness.create(request, scope(), ADDRESS)).job.job_id


def harness_generating(*notes_to_block: int) -> Harness:
    harness = Harness()
    harness.providers.result = result_with(*NOTES)
    harness.providers.unsafe_notes = {NOTES[index].client_id for index in notes_to_block}
    return harness


async def test_one_adversarial_note_among_several_is_dropped_and_the_job_still_succeeds() -> None:
    harness = harness_generating(2)
    job_id = await create(harness, WITHOUT_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Succeeded)
    assert job.state.result.notes == (NOTES[0], NOTES[1], NOTES[3])
    assert ADVERSARIAL not in job.state.result.notes
    assert harness.providers.screened == [result_with(*NOTES)]


async def test_dropped_note_is_screened_before_any_image_is_searched_for_it() -> None:
    harness = harness_generating(2)
    job_id = await create(harness, WITH_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Succeeded)
    kept = (NOTES[0], NOTES[1], NOTES[3])
    assert harness.providers.received[JobStage.FETCHING_MEDIA] == tuple(image_query(note) for note in kept)
    assert job.state.result.notes == with_images(kept)


async def test_only_the_screened_result_is_cached() -> None:
    harness = harness_generating(2)
    job_id = await create(harness, WITHOUT_IMAGES)

    await harness.run(job_id)

    [(_, cached)] = harness.cache.writes
    assert cached.notes == (NOTES[0], NOTES[1], NOTES[3])


@pytest.mark.parametrize("job_request", [WITH_IMAGES, WITHOUT_IMAGES], ids=["with images", "without images"])
async def test_deck_with_nothing_safe_left_fails_with_no_valid_content_before_any_media(
    job_request: GenerationRequest,
) -> None:
    harness = harness_generating(0, 1, 2, 3)
    job_id = await create(harness, job_request)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (FailureCode.NO_VALID_CONTENT, JobStage.GENERATING_CARDS)
    assert harness.providers.fetched_for == []
    assert harness.cache.writes == []


async def test_generation_that_leaves_no_note_is_not_sent_for_screening() -> None:
    harness = Harness()
    harness.providers.result = result_with()
    job_id = await create(harness, WITHOUT_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.NO_VALID_CONTENT
    assert harness.providers.screened == []


async def test_classifier_outage_fails_the_job_as_provider_unavailable_and_ships_nothing_unscreened() -> None:
    harness = harness_generating()
    harness.providers.screen_failure = UpstreamUnavailableError(5)
    job_id = await create(harness, WITH_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (FailureCode.PROVIDER_UNAVAILABLE, JobStage.GENERATING_CARDS)
    assert harness.providers.fetched_for == []
    assert harness.cache.writes == []


async def test_unexpected_classifier_failure_fails_the_job_as_generation_failed() -> None:
    harness = harness_generating()
    harness.providers.screen_failure = RuntimeError("classifier bug")
    job_id = await create(harness, WITHOUT_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.GENERATION_FAILED


async def test_cached_result_is_served_without_screening_it_again() -> None:
    harness = harness_generating()
    first = await create(harness, WITHOUT_IMAGES)
    await harness.run(first)
    harness.providers.screened.clear()
    second = (await harness.create(WITHOUT_IMAGES, scope(client=2), ADDRESS)).job.job_id

    await harness.run(second)

    job = await harness.get(second)
    assert isinstance(job.state, Succeeded)
    assert harness.providers.screened == []
