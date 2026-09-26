import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from itertools import pairwise
from uuid import UUID

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import JobTransition
from deckly.domain.exceptions import InvalidJobTransitionError, JobAlreadyTerminalError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import (
    STAGE_ORDER,
    Cancelled,
    Failed,
    FailureCode,
    GenerationJob,
    JobStage,
    JobStatus,
    Succeeded,
)
from tests.domain.builders import T0, at, basic_note, result_with
from tests.fakes import (
    GENERATED,
    MATERIAL,
    PAGES,
    Harness,
    Hook,
    InMemoryJobStore,
    generation_request,
    scope,
    with_images,
)

pytestmark = pytest.mark.anyio

PORT_STAGES = (
    JobStage.RETRIEVING_SOURCES,
    JobStage.PARSING_SOURCES,
    JobStage.GENERATING_CARDS,
    JobStage.FETCHING_MEDIA,
)
WITH_IMAGES = replace(generation_request(), include_images=True)
WITHOUT_IMAGES = replace(generation_request(), include_images=False)
REQUESTS = {"with images": WITH_IMAGES, "without images": WITHOUT_IMAGES}


class ProviderTimedOutError(UpstreamUnavailableError):
    pass


class CancelsBeforeUpdate(InMemoryJobStore):
    def __init__(self, update_number: int) -> None:
        super().__init__()
        self.update_number = update_number
        self.worker_updates = 0

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        self.worker_updates += 1
        if self.worker_updates == self.update_number:
            await super().update(job_id, lambda job: job.cancel(T0))
        return await super().update(job_id, transition)


class ClaimedElsewhereFirst(InMemoryJobStore):
    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        if not self.history:
            await super().update(job_id, lambda job: job.start(T0))
        return await super().update(job_id, transition)


class UnreachableJobStore(InMemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.reachable = True

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        if not self.reachable:
            message = "job store unreachable"
            raise ConnectionError(message)
        return await super().update(job_id, transition)


async def create(harness: Harness, request: GenerationRequest) -> UUID:
    return (await harness.create(request, scope())).job.job_id


def calls_through(stage: JobStage) -> list[JobStage]:
    return list(PORT_STAGES[: PORT_STAGES.index(stage) + 1])


async def test_job_moves_through_every_stage_in_order_and_succeeds_with_the_enriched_result() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Succeeded)
    assert job.state.result == replace(GENERATED, notes=with_images(GENERATED.notes))
    assert [entry.stage for entry in harness.store.history] == [*STAGE_ORDER, None]
    assert harness.providers.calls == list(PORT_STAGES)


async def test_each_port_receives_what_the_previous_stage_produced() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)

    await harness.run(job_id)

    assert harness.providers.received == {
        JobStage.RETRIEVING_SOURCES: WITH_IMAGES,
        JobStage.PARSING_SOURCES: PAGES,
        JobStage.GENERATING_CARDS: MATERIAL,
        JobStage.FETCHING_MEDIA: GENERATED.notes,
    }


async def test_card_generator_is_told_which_job_it_generates_for() -> None:
    harness = Harness()
    job_id = await create(harness, WITHOUT_IMAGES)

    await harness.run(job_id)

    assert harness.providers.generated_for == [job_id]


async def test_source_retriever_and_parser_are_told_which_job_they_work_for() -> None:
    harness = Harness()
    job_id = await create(harness, WITHOUT_IMAGES)

    await harness.run(job_id)

    assert (harness.providers.retrieved_for, harness.providers.parsed_for) == ([job_id], [job_id])


@pytest.mark.parametrize("job_request", REQUESTS.values(), ids=REQUESTS.keys())
async def test_progress_starts_at_zero_rises_with_every_update_and_ends_complete(
    job_request: GenerationRequest,
) -> None:
    harness = Harness()
    job_id = await create(harness, job_request)

    await harness.run(job_id)

    progress = [entry.progress.value for entry in harness.store.history]
    assert (progress[0], progress[-1]) == (0.0, 1.0)
    assert all(earlier < later for earlier, later in pairwise(progress))


async def test_job_without_images_never_enters_fetching_media_nor_calls_the_fetcher() -> None:
    harness = Harness()
    job_id = await create(harness, WITHOUT_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Succeeded)
    assert job.state.result == GENERATED
    assert JobStage.FETCHING_MEDIA not in harness.providers.calls
    assert JobStage.FETCHING_MEDIA not in {entry.stage for entry in harness.store.history}


FAILURES: dict[str, tuple[Callable[[], Exception], FailureCode]] = {
    "provider unavailable": (lambda: UpstreamUnavailableError(30), FailureCode.PROVIDER_UNAVAILABLE),
    "provider-specific unavailability": (lambda: ProviderTimedOutError(30), FailureCode.PROVIDER_UNAVAILABLE),
    "unexpected error": (lambda: RuntimeError("model client crashed"), FailureCode.GENERATION_FAILED),
    "job state error raised by a port": (
        lambda: JobAlreadyTerminalError("confused adapter"),
        FailureCode.GENERATION_FAILED,
    ),
}


@pytest.mark.parametrize("stage", PORT_STAGES)
@pytest.mark.parametrize(("make_error", "code"), FAILURES.values(), ids=FAILURES.keys())
async def test_port_failure_fails_the_job_at_its_stage_without_raising_or_calling_later_ports(
    stage: JobStage, make_error: Callable[[], Exception], code: FailureCode
) -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    harness.providers.failures[stage] = make_error()

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (code, stage)
    assert harness.providers.calls == calls_through(stage)


async def test_unexpected_port_error_is_logged_with_its_traceback_and_job_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    error = RuntimeError("model client crashed")
    harness.providers.failures[JobStage.GENERATING_CARDS] = error

    with caplog.at_level(logging.INFO, logger="deckly.application.pipeline"):
        await harness.run(job_id)

    [record] = [record for record in caplog.records if record.getMessage() == "generation_failed"]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is error
    assert record.__dict__["job_id"] == str(job_id)
    assert record.__dict__["failure_code"] == FailureCode.GENERATION_FAILED


@pytest.mark.parametrize("job_request", REQUESTS.values(), ids=REQUESTS.keys())
async def test_generation_that_leaves_no_valid_note_fails_with_no_valid_content_before_any_media(
    job_request: GenerationRequest,
) -> None:
    harness = Harness()
    job_id = await create(harness, job_request)
    harness.providers.result = result_with()

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (FailureCode.NO_VALID_CONTENT, JobStage.GENERATING_CARDS)
    assert harness.providers.calls == calls_through(JobStage.GENERATING_CARDS)


async def test_a_single_valid_note_is_enough_to_succeed() -> None:
    harness = Harness()
    job_id = await create(harness, WITHOUT_IMAGES)
    harness.providers.result = result_with(basic_note(1))

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Succeeded)
    assert job.state.result.notes == (basic_note(1),)


async def test_media_stage_that_drops_every_note_fails_with_no_valid_content() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    harness.providers.enrich = lambda _: ()

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (FailureCode.NO_VALID_CONTENT, JobStage.FETCHING_MEDIA)


async def test_media_stage_that_duplicates_a_note_fails_the_job_instead_of_raising() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    harness.providers.enrich = lambda notes: (*notes, notes[0])

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (FailureCode.GENERATION_FAILED, JobStage.FETCHING_MEDIA)


async def test_job_cancelled_before_pickup_is_skipped_and_no_port_is_called() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    cancelled = await harness.cancel(job_id)
    updates = harness.store.update_calls

    await harness.run(job_id)

    assert await harness.get(job_id) == cancelled
    assert harness.store.update_calls == updates
    assert harness.providers.calls == []


FINISHED_BEFORE_PICKUP: dict[str, JobTransition] = {
    "failed": lambda job: job.start(at(1)).fail(FailureCode.PROVIDER_UNAVAILABLE, at(2)),
    "succeeded": lambda job: job.start(at(1)).succeed(GENERATED, at(2)),
}


@pytest.mark.parametrize("finish", FINISHED_BEFORE_PICKUP.values(), ids=FINISHED_BEFORE_PICKUP.keys())
async def test_redelivered_task_of_a_finished_job_leaves_it_untouched(finish: JobTransition) -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    finished = await harness.store.update(job_id, finish)
    updates = harness.store.update_calls

    await harness.run(job_id)

    assert await harness.get(job_id) == finished
    assert harness.store.update_calls == updates
    assert harness.providers.calls == []


CANCELLED_BEFORE_UPDATE: dict[str, tuple[int, JobStage | None, tuple[JobStage, ...]]] = {
    "the claim": (1, None, ()),
    "entering retrieving_sources": (2, JobStage.PLANNING, ()),
    "entering parsing_sources": (3, JobStage.RETRIEVING_SOURCES, PORT_STAGES[:1]),
    "entering generating_cards": (4, JobStage.PARSING_SOURCES, PORT_STAGES[:2]),
    "entering fetching_media": (5, JobStage.GENERATING_CARDS, PORT_STAGES[:3]),
    "entering finalizing": (6, JobStage.FETCHING_MEDIA, PORT_STAGES),
    "recording the result": (7, JobStage.FINALIZING, PORT_STAGES),
}


@pytest.mark.parametrize(
    ("update_number", "stage", "calls"), CANCELLED_BEFORE_UPDATE.values(), ids=CANCELLED_BEFORE_UPDATE.keys()
)
async def test_cancel_that_wins_the_row_before_a_worker_update_stops_the_job_right_there(
    update_number: int, stage: JobStage | None, calls: tuple[JobStage, ...]
) -> None:
    store = CancelsBeforeUpdate(update_number)
    harness = Harness(store)
    job_id = await create(harness, WITH_IMAGES)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Cancelled)
    assert job.stage is stage
    assert tuple(harness.providers.calls) == calls
    assert store.worker_updates == update_number


@pytest.mark.parametrize("stage", PORT_STAGES)
async def test_cancel_during_a_port_call_stops_the_job_before_the_next_port(stage: JobStage) -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    harness.providers.during[stage] = lambda: harness.cancel(job_id)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Cancelled)
    assert job.stage is stage
    assert harness.providers.calls == calls_through(stage)


async def test_port_failure_after_the_user_cancelled_leaves_the_job_cancelled() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    harness.providers.during[JobStage.GENERATING_CARDS] = lambda: harness.cancel(job_id)
    harness.providers.failures[JobStage.GENERATING_CARDS] = UpstreamUnavailableError(30)

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Cancelled)
    assert job.stage is JobStage.GENERATING_CARDS


async def test_job_already_running_when_picked_up_is_failed_as_abandoned_without_calling_a_port() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)
    await harness.store.update(
        job_id, lambda job: job.start(at(1)).advance(JobStage.PARSING_SOURCES, 0.3, at(2))
    )

    await harness.run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage, job.progress.value) == (
        FailureCode.GENERATION_FAILED,
        JobStage.PARSING_SOURCES,
        0.3,
    )
    assert harness.providers.calls == []


async def test_worker_that_loses_the_claim_to_another_raises_and_leaves_the_job_alone() -> None:
    store = ClaimedElsewhereFirst()
    harness = Harness(store)
    job_id = await create(harness, WITH_IMAGES)

    with pytest.raises(InvalidJobTransitionError):
        await harness.run(job_id)

    job = await harness.get(job_id)
    assert (job.status, job.stage) == (JobStatus.RUNNING, JobStage.PLANNING)
    assert harness.providers.calls == []


async def test_unknown_job_is_ignored() -> None:
    harness = Harness()

    await harness.run(UUID(int=999, version=4))

    assert harness.providers.calls == []
    assert harness.store.history == []


async def test_job_row_that_disappears_mid_run_stops_the_pipeline_quietly() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)

    async def delete_the_row() -> None:
        del harness.store.jobs[job_id]

    harness.providers.during[JobStage.PARSING_SOURCES] = delete_the_row

    await harness.run(job_id)

    assert harness.providers.calls == calls_through(JobStage.PARSING_SOURCES)
    assert job_id not in harness.store.jobs


async def test_store_outage_while_recording_a_failure_is_raised_rather_than_swallowed() -> None:
    store = UnreachableJobStore()
    harness = Harness(store)
    job_id = await create(harness, WITH_IMAGES)

    async def lose_the_store() -> None:
        store.reachable = False

    harness.providers.during[JobStage.GENERATING_CARDS] = lose_the_store
    harness.providers.failures[JobStage.GENERATING_CARDS] = RuntimeError("model client crashed")

    with pytest.raises(ConnectionError):
        await harness.run(job_id)


async def interrupt_during_generation(harness: Harness, job_id: UUID, before_hanging: Hook) -> None:
    reached = asyncio.Event()

    async def hang() -> None:
        await before_hanging()
        reached.set()
        await asyncio.Event().wait()

    harness.providers.during[JobStage.GENERATING_CARDS] = hang
    running = asyncio.create_task(harness.run(job_id))
    await reached.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


async def test_worker_interruption_mid_stage_records_generation_failed_and_propagates() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)

    await interrupt_during_generation(harness, job_id, lambda: asyncio.sleep(0))

    job = await harness.get(job_id)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (FailureCode.GENERATION_FAILED, JobStage.GENERATING_CARDS)
    assert harness.providers.calls == calls_through(JobStage.GENERATING_CARDS)


async def test_worker_interruption_of_a_job_the_user_already_cancelled_keeps_it_cancelled() -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)

    await interrupt_during_generation(harness, job_id, lambda: harness.cancel(job_id))

    job = await harness.get(job_id)
    assert isinstance(job.state, Cancelled)
    assert job.stage is JobStage.GENERATING_CARDS


async def test_interruption_of_a_cancelled_job_is_logged_as_stopped(caplog: pytest.LogCaptureFixture) -> None:
    harness = Harness()
    job_id = await create(harness, WITH_IMAGES)

    with caplog.at_level(logging.INFO, logger="deckly.application.pipeline"):
        await interrupt_during_generation(harness, job_id, lambda: harness.cancel(job_id))

    [record] = [record for record in caplog.records if record.getMessage() == "generation_stopped"]
    assert record.__dict__["job_id"] == str(job_id)


async def test_worker_interruption_still_propagates_when_the_failure_cannot_be_recorded() -> None:
    store = UnreachableJobStore()
    harness = Harness(store)
    job_id = await create(harness, WITH_IMAGES)

    async def lose_the_store() -> None:
        store.reachable = False

    await interrupt_during_generation(harness, job_id, lose_the_store)

    assert (await harness.get(job_id)).status is JobStatus.RUNNING
