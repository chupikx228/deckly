import logging
from collections.abc import Callable
from dataclasses import replace
from uuid import UUID

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.deck import GenerationResult
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import Cancelled, Failed, JobStage, JobStatus, Succeeded
from deckly.domain.notes.note_type import NoteType
from tests.domain.builders import T0, basic_note, result_with
from tests.fakes import (
    ADDRESS,
    GENERATED,
    Harness,
    InMemoryResultCache,
    generation_request,
    scope,
    with_images,
)

pytestmark = pytest.mark.anyio

PIPELINE_LOGGER = "deckly.application.pipeline"
CACHED = result_with(basic_note(41), basic_note(42))
WITH_IMAGES = replace(generation_request(), include_images=True)
MULTI_TYPE = replace(
    generation_request(), note_types=(NoteType.BASIC, NoteType.CLOZE, NoteType.MULTIPLE_CHOICE)
)
PIPELINE_STAGES = (JobStage.RETRIEVING_SOURCES, JobStage.PARSING_SOURCES, JobStage.GENERATING_CARDS)
CACHE_FAILURES: dict[str, Callable[[], Exception]] = {
    "upstream unavailable": lambda: UpstreamUnavailableError(1),
    "timeout": lambda: TimeoutError("redis did not answer"),
    "unexpected": lambda: RuntimeError("cache client bug"),
}


class StatusRecordingCache(InMemoryResultCache):
    def __init__(self, harness: Harness) -> None:
        super().__init__()
        self.harness = harness
        self.seen: list[JobStatus] = []

    async def get(self, request: GenerationRequest) -> GenerationResult | None:
        self.seen.extend(job.status for job in self.harness.store.jobs.values())
        return await super().get(request)


class CancellingCache(InMemoryResultCache):
    def __init__(self, harness: Harness, job_id: UUID) -> None:
        super().__init__()
        self.harness = harness
        self.job_id = job_id

    async def get(self, request: GenerationRequest) -> GenerationResult | None:
        await self.harness.cancel(self.job_id)
        return await super().get(request)


async def submit(harness: Harness, request: GenerationRequest, client: int = 1) -> UUID:
    created = await harness.create(request, scope(client, key=client), ADDRESS)
    return created.job.job_id


async def run(harness: Harness, request: GenerationRequest, client: int = 1) -> UUID:
    job_id = await submit(harness, request, client)
    await harness.run(job_id)
    return job_id


async def succeeded_result(harness: Harness, job_id: UUID) -> GenerationResult:
    job = await harness.get(job_id)
    assert isinstance(job.state, Succeeded)
    return job.state.result


async def test_a_miss_runs_the_pipeline_and_stores_the_result_under_the_request() -> None:
    harness = Harness()

    job_id = await run(harness, MULTI_TYPE)

    assert await succeeded_result(harness, job_id) == GENERATED
    assert harness.cache.reads == [MULTI_TYPE]
    assert harness.cache.writes == [(MULTI_TYPE, GENERATED)]
    assert harness.providers.calls == list(PIPELINE_STAGES)


async def test_a_hit_returns_the_cached_result_without_calling_any_provider() -> None:
    harness = Harness()
    harness.cache.entries[MULTI_TYPE.fingerprint()] = CACHED

    job_id = await run(harness, MULTI_TYPE)

    assert await succeeded_result(harness, job_id) == CACHED
    assert harness.providers.calls == []
    assert harness.cache.writes == []


async def test_a_hit_goes_through_the_normal_state_machine_without_a_special_status() -> None:
    harness = Harness()
    harness.cache.entries[MULTI_TYPE.fingerprint()] = CACHED

    job_id = await submit(harness, MULTI_TYPE)
    queued = await harness.get(job_id)
    await harness.run(job_id)

    assert queued.status is JobStatus.QUEUED
    assert [(entry.status, entry.stage) for entry in harness.store.history] == [
        (JobStatus.RUNNING, JobStage.PLANNING),
        (JobStatus.SUCCEEDED, None),
    ]
    assert harness.store.history[-1].progress.value == 1.0


async def test_the_cache_is_consulted_only_once_the_job_is_running() -> None:
    harness = Harness()
    cache = StatusRecordingCache(harness)
    harness.run = replace(harness.run, cache=cache)

    await run(harness, MULTI_TYPE)

    assert cache.seen == [JobStatus.RUNNING]


async def test_a_second_client_asking_for_the_same_deck_is_served_from_the_first_run() -> None:
    harness = Harness()
    first = await run(harness, MULTI_TYPE, client=1)
    calls_after_first = list(harness.providers.calls)

    second = await run(harness, MULTI_TYPE, client=2)

    assert first != second
    assert await succeeded_result(harness, second) == await succeeded_result(harness, first)
    assert harness.providers.calls == calls_after_first
    assert len(harness.cache.writes) == 1


async def test_requests_that_normalize_to_the_same_tuple_hit_the_same_entry() -> None:
    harness = Harness()
    await run(harness, MULTI_TYPE, client=1)
    calls_after_first = list(harness.providers.calls)
    variants = [
        replace(MULTI_TYPE, note_types=tuple(reversed(MULTI_TYPE.note_types))),
        replace(MULTI_TYPE, topic="  Road   signs\n"),
        replace(MULTI_TYPE, language="RU"),
        replace(MULTI_TYPE, instructions="  "),
    ]

    for client, variant in enumerate(variants, start=2):
        job_id = await run(harness, variant, client=client)
        assert await succeeded_result(harness, job_id) == GENERATED

    assert harness.providers.calls == calls_after_first
    assert len(harness.cache.writes) == 1


@pytest.mark.parametrize(
    "different",
    [
        replace(MULTI_TYPE, topic="Road rules"),
        replace(MULTI_TYPE, language="en"),
        replace(MULTI_TYPE, card_count=41),
        replace(MULTI_TYPE, note_types=(NoteType.BASIC,)),
        replace(MULTI_TYPE, include_images=True),
        replace(MULTI_TYPE, instructions="Only European signs"),
    ],
    ids=["topic", "language", "card_count", "note_types", "include_images", "instructions"],
)
async def test_a_request_that_differs_in_any_output_shaping_field_runs_the_pipeline(
    different: GenerationRequest,
) -> None:
    harness = Harness()
    await run(harness, MULTI_TYPE, client=1)
    calls_after_first = len(harness.providers.calls)

    await run(harness, different, client=2)

    assert len(harness.providers.calls) > calls_after_first
    assert len(harness.cache.entries) == 2


async def test_a_deck_generated_without_images_is_never_served_to_a_request_for_images() -> None:
    harness = Harness()
    await run(harness, MULTI_TYPE, client=1)

    job_id = await run(harness, replace(MULTI_TYPE, include_images=True), client=2)

    assert await succeeded_result(harness, job_id) == replace(GENERATED, notes=with_images(GENERATED.notes))


@pytest.mark.parametrize("failure", CACHE_FAILURES.values(), ids=CACHE_FAILURES.keys())
async def test_a_cache_that_cannot_be_read_does_not_fail_the_job_and_the_pipeline_runs(
    failure: Callable[[], Exception],
) -> None:
    harness = Harness()
    harness.cache.read_failure = failure()

    job_id = await run(harness, MULTI_TYPE)

    assert await succeeded_result(harness, job_id) == GENERATED
    assert harness.providers.calls == list(PIPELINE_STAGES)
    assert harness.cache.writes == [(MULTI_TYPE, GENERATED)]


@pytest.mark.parametrize("failure", CACHE_FAILURES.values(), ids=CACHE_FAILURES.keys())
async def test_a_cache_that_cannot_be_written_does_not_fail_a_job_that_has_succeeded(
    failure: Callable[[], Exception],
) -> None:
    harness = Harness()
    harness.cache.write_failure = failure()

    job_id = await run(harness, MULTI_TYPE)

    assert await succeeded_result(harness, job_id) == GENERATED
    assert harness.cache.entries == {}


async def test_a_cache_that_is_down_for_both_reading_and_writing_changes_nothing_for_the_job() -> None:
    harness = Harness()
    harness.cache.read_failure = UpstreamUnavailableError(1)
    harness.cache.write_failure = UpstreamUnavailableError(1)

    job_id = await run(harness, MULTI_TYPE)

    assert await succeeded_result(harness, job_id) == GENERATED
    assert [entry.stage for entry in harness.store.history] == [
        JobStage.PLANNING,
        *PIPELINE_STAGES,
        JobStage.FINALIZING,
        None,
    ]


@pytest.mark.parametrize(
    ("failure", "level"),
    [(UpstreamUnavailableError(1), logging.WARNING), (RuntimeError("cache client bug"), logging.ERROR)],
    ids=["expected outage", "unexpected bug"],
)
async def test_cache_failures_are_logged_at_a_level_matching_how_expected_they_were(
    caplog: pytest.LogCaptureFixture, failure: Exception, level: int
) -> None:
    harness = Harness()
    harness.cache.read_failure = failure
    harness.cache.write_failure = failure

    with caplog.at_level(logging.INFO, logger=PIPELINE_LOGGER):
        await run(harness, MULTI_TYPE)

    records = {
        record.getMessage(): record.levelno
        for record in caplog.records
        if record.getMessage().startswith("generation_cache")
    }
    assert records == {"generation_cache_read_failed": level, "generation_cache_write_failed": level}


async def test_a_hit_and_a_miss_are_told_apart_in_the_success_log(caplog: pytest.LogCaptureFixture) -> None:
    harness = Harness()

    with caplog.at_level(logging.INFO, logger=PIPELINE_LOGGER):
        await run(harness, MULTI_TYPE, client=1)
        await run(harness, MULTI_TYPE, client=2)

    outcomes = [
        getattr(record, "cache_hit", None)
        for record in caplog.records
        if record.getMessage() == "generation_succeeded"
    ]
    assert outcomes == [False, True]


async def test_a_failed_generation_is_never_cached() -> None:
    harness = Harness()
    harness.providers.failures[JobStage.GENERATING_CARDS] = UpstreamUnavailableError(1)

    job_id = await run(harness, MULTI_TYPE)

    assert isinstance((await harness.get(job_id)).state, Failed)
    assert harness.cache.writes == []


async def test_a_generation_with_no_valid_note_is_never_cached() -> None:
    harness = Harness()
    harness.providers.result = result_with()

    job_id = await run(harness, MULTI_TYPE)

    assert isinstance((await harness.get(job_id)).state, Failed)
    assert harness.cache.writes == []


async def test_a_job_cancelled_while_generating_is_never_cached() -> None:
    harness = Harness()
    job_id = await submit(harness, MULTI_TYPE)

    async def cancel() -> None:
        await harness.cancel(job_id)

    harness.providers.during[JobStage.GENERATING_CARDS] = cancel

    await harness.run(job_id)

    assert isinstance((await harness.get(job_id)).state, Cancelled)
    assert harness.cache.writes == []


async def test_a_job_cancelled_while_the_cache_is_read_stays_cancelled_even_on_a_hit() -> None:
    harness = Harness()
    job_id = await submit(harness, MULTI_TYPE)
    cache = CancellingCache(harness, job_id)
    cache.entries[MULTI_TYPE.fingerprint()] = CACHED
    harness.run = replace(harness.run, cache=cache)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Cancelled)
    assert harness.providers.calls == []


async def test_a_job_another_worker_already_claimed_does_not_consult_the_cache() -> None:
    harness = Harness()
    job_id = await submit(harness, MULTI_TYPE)
    claimed = await harness.get(job_id)
    harness.store.replace(claimed.start(T0))

    await harness.run(job_id)

    assert harness.cache.reads == []
    assert isinstance((await harness.get(job_id)).state, Failed)


async def test_a_deck_with_images_is_cached_together_with_its_media() -> None:
    harness = Harness()

    first = await run(harness, WITH_IMAGES, client=1)
    second = await run(harness, WITH_IMAGES, client=2)

    expected = replace(GENERATED, notes=with_images(GENERATED.notes))
    assert await succeeded_result(harness, first) == expected
    assert await succeeded_result(harness, second) == expected
    assert harness.cache.writes == [(WITH_IMAGES, expected)]
    assert len(harness.providers.fetched_for) == len(harness.providers.generated_for) == 1


async def test_a_deck_whose_media_step_failed_is_returned_but_not_cached_so_the_outage_is_not_pinned() -> (
    None
):
    harness = Harness()
    harness.providers.failures[JobStage.FETCHING_MEDIA] = UpstreamUnavailableError(1)

    first = await run(harness, WITH_IMAGES, client=1)

    assert await succeeded_result(harness, first) == GENERATED
    assert harness.cache.writes == []

    harness.providers.failures.clear()
    second = await run(harness, WITH_IMAGES, client=2)

    assert await succeeded_result(harness, second) == replace(GENERATED, notes=with_images(GENERATED.notes))
    assert len(harness.cache.writes) == 1


async def test_a_deck_that_asked_for_no_images_is_cached_even_though_the_media_stage_never_ran() -> None:
    harness = Harness()
    harness.providers.failures[JobStage.FETCHING_MEDIA] = UpstreamUnavailableError(1)

    await run(harness, MULTI_TYPE)

    assert len(harness.cache.writes) == 1
