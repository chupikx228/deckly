from datetime import timedelta
from uuid import UUID

import pytest

from deckly.application.housekeeping import (
    EnforceJobRetention,
    RetentionOutcome,
    RetentionPolicy,
    StalenessPolicy,
    SweepStaleJobs,
)
from deckly.application.ports import JobTransition
from deckly.domain.exceptions import JobNotFoundError
from deckly.domain.job import Failed, FailureCode, GenerationJob, JobStage, JobStatus
from tests.domain.builders import FULL_RESULT, T0
from tests.fakes import (
    ADDRESS,
    Harness,
    InMemoryJobHousekeeping,
    InMemoryJobStore,
    generation_request,
    job_id,
    scope,
)

pytestmark = pytest.mark.anyio

RUNNING_AFTER = timedelta(minutes=15)
QUEUED_AFTER = timedelta(hours=1)
IDEMPOTENCY_KEY_TTL = timedelta(hours=24)
JOB_RETENTION = timedelta(hours=48)
BATCH_SIZE = 10
TICK = timedelta(microseconds=1)


class UnrestorableJobStore(InMemoryJobStore):
    def __init__(self, unrestorable: UUID) -> None:
        super().__init__()
        self.unrestorable = unrestorable

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        if job_id == self.unrestorable:
            message = f"stored job {job_id} cannot be restored"
            raise ValueError(message)
        return await super().update(job_id, transition)


class Sweeper:
    def __init__(self, batch_size: int = BATCH_SIZE, store: InMemoryJobStore | None = None) -> None:
        self.harness = Harness(store)
        self.store = self.harness.store
        self.housekeeping = InMemoryJobHousekeeping(self.store)
        self.sweep = SweepStaleJobs(
            store=self.store,
            housekeeping=self.housekeeping,
            policy=StalenessPolicy(running_after=RUNNING_AFTER, queued_after=QUEUED_AFTER),
            batch_size=batch_size,
            clock=lambda: self.harness.now,
        )
        self.retention = EnforceJobRetention(
            housekeeping=self.housekeeping,
            policy=RetentionPolicy(idempotency_key_ttl=IDEMPOTENCY_KEY_TTL, job_retention=JOB_RETENTION),
            batch_size=batch_size,
            clock=lambda: self.harness.now,
        )

    def put(self, job: GenerationJob) -> UUID:
        self.store.replace(job)
        return job.job_id

    def job(self, stored_id: UUID) -> GenerationJob:
        return self.store.jobs[stored_id]


def queued(number: int, at: timedelta = timedelta()) -> GenerationJob:
    return GenerationJob.queue(job_id(number), T0 + at)


def running(number: int, at: timedelta = timedelta()) -> GenerationJob:
    return queued(number).start(T0).advance(JobStage.GENERATING_CARDS, 0.5, T0 + at)


async def test_running_job_silent_past_the_threshold_is_failed_where_it_stopped() -> None:
    sweeper = Sweeper()
    stuck = sweeper.put(running(1))
    sweeper.harness.now = T0 + RUNNING_AFTER + TICK

    assert await sweeper.sweep() == 1

    job = sweeper.job(stuck)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage, job.progress.value) == (
        FailureCode.GENERATION_FAILED,
        JobStage.GENERATING_CARDS,
        0.5,
    )
    assert job.updated_at == sweeper.harness.now


async def test_running_job_silent_for_exactly_the_threshold_is_left_alone() -> None:
    sweeper = Sweeper()
    alive = sweeper.put(running(1))
    sweeper.harness.now = T0 + RUNNING_AFTER

    assert await sweeper.sweep() == 0

    assert sweeper.job(alive).status is JobStatus.RUNNING


async def test_staleness_is_measured_from_the_last_state_change_not_from_creation() -> None:
    sweeper = Sweeper()
    progressing = sweeper.put(running(1, at=timedelta(minutes=10)))
    sweeper.harness.now = T0 + RUNNING_AFTER + TICK

    assert await sweeper.sweep() == 0

    assert sweeper.job(progressing).status is JobStatus.RUNNING


async def test_queued_job_waits_longer_than_a_running_one_before_it_is_failed() -> None:
    sweeper = Sweeper()
    waiting = sweeper.put(queued(1))
    sweeper.harness.now = T0 + QUEUED_AFTER

    assert await sweeper.sweep() == 0
    assert sweeper.job(waiting).status is JobStatus.QUEUED

    sweeper.harness.now = T0 + QUEUED_AFTER + TICK

    assert await sweeper.sweep() == 1
    job = sweeper.job(waiting)
    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage, job.progress.value) == (FailureCode.GENERATION_FAILED, None, 0.0)


async def test_terminal_jobs_are_never_touched_by_the_sweep() -> None:
    sweeper = Sweeper()
    finished = [
        sweeper.put(running(1).succeed(FULL_RESULT, T0)),
        sweeper.put(running(2).cancel(T0)),
        sweeper.put(queued(3).fail(FailureCode.NO_VALID_CONTENT, T0)),
    ]
    before = [sweeper.job(stored_id) for stored_id in finished]
    sweeper.harness.now = T0 + timedelta(days=30)

    assert await sweeper.sweep() == 0

    assert [sweeper.job(stored_id) for stored_id in finished] == before


async def test_queued_job_claimed_by_a_worker_after_it_was_listed_is_left_running() -> None:
    sweeper = Sweeper()
    claimed = sweeper.put(queued(1))
    sweeper.harness.now = T0 + QUEUED_AFTER + TICK
    sweeper.housekeeping.after_listing[JobStatus.QUEUED] = lambda: sweeper.put(
        sweeper.job(claimed).start(sweeper.harness.now)
    )

    assert await sweeper.sweep() == 0

    assert sweeper.job(claimed).status is JobStatus.RUNNING


async def test_running_job_that_advanced_after_it_was_listed_is_left_running() -> None:
    sweeper = Sweeper()
    advanced = sweeper.put(running(1))
    sweeper.harness.now = T0 + RUNNING_AFTER + TICK
    sweeper.housekeeping.after_listing[JobStatus.RUNNING] = lambda: sweeper.put(
        sweeper.job(advanced).advance(JobStage.FINALIZING, 0.9, sweeper.harness.now)
    )

    assert await sweeper.sweep() == 0

    job = sweeper.job(advanced)
    assert (job.status, job.stage) == (JobStatus.RUNNING, JobStage.FINALIZING)


async def test_job_that_finished_after_it_was_listed_keeps_its_result() -> None:
    sweeper = Sweeper()
    finished = sweeper.put(running(1))
    sweeper.harness.now = T0 + RUNNING_AFTER + TICK
    sweeper.housekeeping.after_listing[JobStatus.RUNNING] = lambda: sweeper.put(
        sweeper.job(finished).succeed(FULL_RESULT, sweeper.harness.now)
    )

    assert await sweeper.sweep() == 0

    assert sweeper.job(finished).status is JobStatus.SUCCEEDED


async def test_job_deleted_after_it_was_listed_is_skipped() -> None:
    sweeper = Sweeper()
    deleted = sweeper.put(running(1))
    sweeper.harness.now = T0 + RUNNING_AFTER + TICK
    sweeper.housekeeping.after_listing[JobStatus.RUNNING] = lambda: sweeper.store.forget(deleted)

    assert await sweeper.sweep() == 0

    assert deleted not in sweeper.store.jobs


async def test_one_run_fails_at_most_one_batch_of_each_status_oldest_first() -> None:
    sweeper = Sweeper(batch_size=2)
    stuck_running = [sweeper.put(running(number, at=timedelta(seconds=number))) for number in (1, 2, 3)]
    stuck_queued = [sweeper.put(queued(number, at=timedelta(seconds=number))) for number in (4, 5, 6)]
    sweeper.harness.now = T0 + QUEUED_AFTER + timedelta(minutes=1)

    assert await sweeper.sweep() == 4

    assert [sweeper.job(stored_id).status for stored_id in stuck_running] == [
        JobStatus.FAILED,
        JobStatus.FAILED,
        JobStatus.RUNNING,
    ]
    assert [sweeper.job(stored_id).status for stored_id in stuck_queued] == [
        JobStatus.FAILED,
        JobStatus.FAILED,
        JobStatus.QUEUED,
    ]

    assert await sweeper.sweep() == 2


async def test_poll_of_a_swept_job_reports_a_failure_instead_of_an_endless_wait() -> None:
    sweeper = Sweeper()
    created = await sweeper.harness.create(generation_request(), scope(), ADDRESS)
    sweeper.harness.now = T0 + QUEUED_AFTER + TICK

    await sweeper.sweep()

    polled = await sweeper.harness.get(created.job.job_id)
    assert isinstance(polled.state, Failed)
    assert polled.state.code is FailureCode.GENERATION_FAILED


async def test_key_older_than_its_ttl_starts_a_new_job_while_the_original_stays_pollable() -> None:
    sweeper = Sweeper()
    original = await sweeper.harness.create(generation_request(), scope(), ADDRESS)
    sweeper.harness.now = T0 + IDEMPOTENCY_KEY_TTL + TICK

    assert await sweeper.retention() == RetentionOutcome(released_keys=1, purged_jobs=0)
    replay = await sweeper.harness.create(generation_request(), scope(), ADDRESS)

    assert replay.job.job_id != original.job.job_id
    assert replay.job.created_at == sweeper.harness.now
    assert await sweeper.harness.get(original.job.job_id) == original.job
    assert (await sweeper.harness.create(generation_request(), scope(), ADDRESS)).job == replay.job


async def test_key_exactly_as_old_as_its_ttl_still_replays_the_original_job() -> None:
    sweeper = Sweeper()
    original = await sweeper.harness.create(generation_request(), scope(), ADDRESS)
    sweeper.harness.now = T0 + IDEMPOTENCY_KEY_TTL

    assert await sweeper.retention() == RetentionOutcome(released_keys=0, purged_jobs=0)

    replay = await sweeper.harness.create(generation_request(), scope(), ADDRESS)
    assert replay.job == original.job


async def test_finished_job_past_its_retention_is_deleted_and_no_longer_found() -> None:
    sweeper = Sweeper()
    expired = sweeper.put(running(1).succeed(FULL_RESULT, T0))
    sweeper.harness.now = T0 + JOB_RETENTION + TICK

    assert await sweeper.retention() == RetentionOutcome(released_keys=0, purged_jobs=1)

    with pytest.raises(JobNotFoundError):
        await sweeper.harness.get(expired)


async def test_retention_is_measured_from_when_the_job_finished_not_when_it_was_created() -> None:
    sweeper = Sweeper()
    finished_late = sweeper.put(running(1).succeed(FULL_RESULT, T0 + timedelta(hours=1)))
    sweeper.harness.now = T0 + JOB_RETENTION + TICK

    assert await sweeper.retention() == RetentionOutcome(released_keys=0, purged_jobs=0)

    assert (await sweeper.harness.get(finished_late)).status is JobStatus.SUCCEEDED


async def test_unfinished_jobs_are_never_deleted_however_old() -> None:
    sweeper = Sweeper()
    unfinished = [sweeper.put(queued(1)), sweeper.put(running(2))]
    sweeper.harness.now = T0 + timedelta(days=365)

    assert (await sweeper.retention()).purged_jobs == 0

    assert all(stored_id in sweeper.store.jobs for stored_id in unfinished)


async def test_one_retention_run_handles_at_most_one_batch_of_keys_and_of_jobs() -> None:
    sweeper = Sweeper(batch_size=2)
    for client in (1, 2, 3):
        await sweeper.harness.create(generation_request(), scope(client=client), ADDRESS)
    for number in (11, 12, 13):
        sweeper.put(queued(number).cancel(T0))
    sweeper.harness.now = T0 + JOB_RETENTION + TICK

    assert await sweeper.retention() == RetentionOutcome(released_keys=2, purged_jobs=2)
    assert await sweeper.retention() == RetentionOutcome(released_keys=1, purged_jobs=1)


async def test_one_job_that_cannot_be_updated_does_not_stop_the_sweep_from_failing_the_rest() -> None:
    poisoned = job_id(1)
    sweeper = Sweeper(store=UnrestorableJobStore(poisoned))
    sweeper.put(running(1))
    stuck = sweeper.put(running(2, at=timedelta(seconds=1)))
    sweeper.harness.now = T0 + RUNNING_AFTER + timedelta(minutes=1)

    assert await sweeper.sweep() == 1

    assert sweeper.job(poisoned).status is JobStatus.RUNNING
    assert sweeper.job(stuck).status is JobStatus.FAILED
