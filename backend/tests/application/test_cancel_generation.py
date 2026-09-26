from collections.abc import Callable
from uuid import UUID

import pytest

from deckly.domain.exceptions import JobAlreadyTerminalError, JobNotFoundError
from deckly.domain.job import FailureCode, GenerationJob, JobStage, JobStatus
from tests.domain.builders import JOB_ID, T0, at, basic_note, result_with
from tests.fakes import Harness, InMemoryJobStore, RecordingJobQueue, generation_request, job_id, scope

pytestmark = pytest.mark.anyio


class StatusSeenOnAbort(RecordingJobQueue):
    def __init__(self, store: InMemoryJobStore) -> None:
        super().__init__()
        self.store = store
        self.statuses: list[JobStatus] = []

    async def abort(self, job_id: UUID) -> None:
        self.statuses.append(self.store.jobs[job_id].status)
        await super().abort(job_id)


def queued() -> GenerationJob:
    return GenerationJob.queue(JOB_ID, T0)


def running() -> GenerationJob:
    return queued().start(at(1)).advance(JobStage.GENERATING_CARDS, 0.62, at(44))


ACTIVE_JOBS: dict[str, Callable[[], GenerationJob]] = {"queued": queued, "running": running}
TERMINAL_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "succeeded": lambda: running().succeed(result_with(basic_note(1)), at(90)),
    "failed": lambda: running().fail(FailureCode.PROVIDER_UNAVAILABLE, at(60)),
    "cancelled": lambda: running().cancel(at(60)),
}


def harness_with(job: GenerationJob) -> Harness:
    harness = Harness()
    harness.store.replace(job)
    harness.now = at(100)
    return harness


@pytest.mark.parametrize("make_job", ACTIVE_JOBS.values(), ids=ACTIVE_JOBS.keys())
async def test_active_job_is_cancelled_and_stored(make_job: Callable[[], GenerationJob]) -> None:
    job = make_job()
    harness = harness_with(job)

    cancelled = await harness.cancel(JOB_ID)

    assert cancelled == job.cancel(at(100))
    assert harness.store.jobs[JOB_ID] == cancelled
    assert cancelled.status is JobStatus.CANCELLED
    assert (cancelled.stage, cancelled.progress) == (job.stage, job.progress)
    assert cancelled.updated_at == at(100)


@pytest.mark.parametrize("make_job", ACTIVE_JOBS.values(), ids=ACTIVE_JOBS.keys())
async def test_work_in_flight_is_aborted_only_once_the_job_is_stored_as_cancelled(
    make_job: Callable[[], GenerationJob],
) -> None:
    store = InMemoryJobStore()
    queue = StatusSeenOnAbort(store)
    harness = Harness(store, queue)
    store.replace(make_job())

    await harness.cancel(JOB_ID)

    assert queue.aborted == [JOB_ID]
    assert queue.statuses == [JobStatus.CANCELLED]


@pytest.mark.parametrize("make_job", TERMINAL_JOBS.values(), ids=TERMINAL_JOBS.keys())
async def test_terminal_job_is_a_conflict_and_left_untouched(make_job: Callable[[], GenerationJob]) -> None:
    job = make_job()
    harness = harness_with(job)

    with pytest.raises(JobAlreadyTerminalError):
        await harness.cancel(JOB_ID)

    assert harness.store.jobs == {JOB_ID: job}
    assert harness.queue.aborted == []


async def test_unknown_job_is_not_found() -> None:
    harness = Harness()
    await harness.create(generation_request(), scope())

    with pytest.raises(JobNotFoundError):
        await harness.cancel(job_id(999))

    assert harness.queue.aborted == []


async def test_job_created_through_the_use_case_can_be_cancelled_once() -> None:
    harness = Harness()
    created = await harness.create(generation_request(), scope())

    await harness.cancel(created.job.job_id)

    with pytest.raises(JobAlreadyTerminalError):
        await harness.cancel(created.job.job_id)
    assert (await harness.get(created.job.job_id)).status is JobStatus.CANCELLED


async def test_clock_behind_the_stored_job_never_moves_updated_at_backwards() -> None:
    job = running()
    harness = harness_with(job)
    harness.now = at(10)

    cancelled = await harness.cancel(JOB_ID)

    assert cancelled.updated_at == job.updated_at
