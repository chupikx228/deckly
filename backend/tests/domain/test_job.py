import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import FrozenInstanceError
from typing import cast
from uuid import UUID

import pytest

from deckly.domain.exceptions import (
    ConflictError,
    InvalidFailureCodeError,
    InvalidJobIdError,
    InvalidJobTransitionError,
    InvalidProgressError,
    InvalidTimestampError,
    JobAlreadyTerminalError,
    ProgressRegressionError,
    StageRegressionError,
)
from deckly.domain.job import (
    STAGE_ORDER,
    Failed,
    FailureCode,
    GenerationJob,
    JobStage,
    JobStatus,
    Progress,
    Queued,
    Running,
)
from tests.domain.builders import JOB_ID, T0, at, basic_note, result_with

type Transition = Callable[[GenerationJob], GenerationJob]

CONTRACT_STAGE_ORDER = [
    "planning",
    "retrieving_sources",
    "parsing_sources",
    "generating_cards",
    "fetching_media",
    "finalizing",
]


def queued() -> GenerationJob:
    return GenerationJob.queue(JOB_ID, T0)


def running(stage: JobStage = JobStage.PLANNING, progress: float = 0.0) -> GenerationJob:
    job = queued().start(at(1))
    return job.advance(stage, progress, at(2))


def succeeded() -> GenerationJob:
    return running(JobStage.FINALIZING, 0.9).succeed(result_with(basic_note(1)), at(3))


def failed() -> GenerationJob:
    return running(JobStage.GENERATING_CARDS, 0.5).fail(FailureCode.NO_VALID_CONTENT, at(3))


def cancelled() -> GenerationJob:
    return running().cancel(at(3))


ALL_TRANSITIONS: dict[str, Transition] = {
    "start": lambda job: job.start(at(10)),
    "advance": lambda job: job.advance(JobStage.FINALIZING, 1.0, at(10)),
    "succeed": lambda job: job.succeed(result_with(basic_note(1)), at(10)),
    "fail": lambda job: job.fail(FailureCode.GENERATION_FAILED, at(10)),
    "cancel": lambda job: job.cancel(at(10)),
}

TERMINAL_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "succeeded": succeeded,
    "failed": failed,
    "cancelled": cancelled,
}


def test_full_lifecycle_reaches_succeeded_with_complete_progress() -> None:
    job = queued().start(at(1))
    for position, stage in enumerate(STAGE_ORDER):
        job = job.advance(stage, position / len(STAGE_ORDER), at(2 + position))

    done = job.succeed(result_with(basic_note(1)), at(20))

    assert done.status is JobStatus.SUCCEEDED
    assert done.is_terminal
    assert done.stage is None
    assert done.progress == Progress(1.0)
    assert done.updated_at == at(20)


def test_new_job_is_queued_with_no_stage_and_zero_progress() -> None:
    job = queued()

    assert job.status is JobStatus.QUEUED
    assert job.stage is None
    assert job.progress == Progress(0.0)
    assert job.created_at == job.updated_at == T0
    assert not job.is_terminal


def test_start_enters_the_first_stage() -> None:
    job = queued().start(at(1))

    assert job.status is JobStatus.RUNNING
    assert job.stage is JobStage.PLANNING


@pytest.mark.parametrize("terminal", TERMINAL_JOBS.values(), ids=TERMINAL_JOBS.keys())
@pytest.mark.parametrize("transition", ALL_TRANSITIONS.values(), ids=ALL_TRANSITIONS.keys())
def test_every_transition_out_of_a_terminal_state_is_rejected(
    terminal: Callable[[], GenerationJob], transition: Transition
) -> None:
    job = terminal()

    with pytest.raises(JobAlreadyTerminalError):
        transition(job)


@pytest.mark.parametrize("terminal", TERMINAL_JOBS.values(), ids=TERMINAL_JOBS.keys())
def test_cancel_on_terminal_job_is_a_conflict(terminal: Callable[[], GenerationJob]) -> None:
    with pytest.raises(ConflictError):
        terminal().cancel(at(10))


@pytest.mark.parametrize("action", ["advance", "succeed"])
def test_queued_job_cannot_skip_running(action: str) -> None:
    with pytest.raises(InvalidJobTransitionError) as caught:
        ALL_TRANSITIONS[action](queued())

    assert not isinstance(caught.value, JobAlreadyTerminalError)


def test_running_job_cannot_start_again() -> None:
    with pytest.raises(InvalidJobTransitionError) as caught:
        running().start(at(10))

    assert not isinstance(caught.value, JobAlreadyTerminalError)


@pytest.mark.parametrize("make_job", [queued, running], ids=["queued", "running"])
def test_active_job_can_be_cancelled(make_job: Callable[[], GenerationJob]) -> None:
    assert make_job().cancel(at(10)).status is JobStatus.CANCELLED


@pytest.mark.parametrize("make_job", [queued, running], ids=["queued", "running"])
def test_active_job_can_fail(make_job: Callable[[], GenerationJob]) -> None:
    assert make_job().fail(FailureCode.GENERATION_FAILED, at(10)).status is JobStatus.FAILED


@pytest.mark.parametrize("code", list(FailureCode))
def test_failed_job_records_its_failure_code(code: FailureCode) -> None:
    job = running().fail(code, at(10))

    assert isinstance(job.state, Failed)
    assert job.state.code is code


@pytest.mark.parametrize(
    "code",
    ["boom", "", "generation_failed", "UPSTREAM_UNAVAILABLE", "VALIDATION_FAILED", "INTERNAL_ERROR"],
)
def test_failing_with_an_unrecognised_code_is_rejected_and_leaves_the_job_running(code: str) -> None:
    job = running()

    with pytest.raises(InvalidFailureCodeError):
        job.fail(cast("FailureCode", code), at(10))

    assert job.status is JobStatus.RUNNING


def test_failure_vocabulary_is_the_contract_one() -> None:
    assert {str(code) for code in FailureCode} == {
        "PROVIDER_UNAVAILABLE",
        "NO_VALID_CONTENT",
        "GENERATION_FAILED",
    }


def test_failed_job_keeps_the_stage_and_progress_it_stopped_at() -> None:
    job = running(JobStage.GENERATING_CARDS, 0.62).fail(FailureCode.GENERATION_FAILED, at(10))

    assert job.stage is JobStage.GENERATING_CARDS
    assert job.progress == Progress(0.62)


def test_cancelling_a_queued_job_has_no_stage_and_zero_progress() -> None:
    job = queued().cancel(at(10))

    assert job.stage is None
    assert job.progress == Progress(0.0)


def test_rejected_transition_leaves_the_job_unchanged() -> None:
    job = succeeded()
    before = (job.state, job.updated_at)

    with pytest.raises(JobAlreadyTerminalError):
        job.cancel(at(99))

    assert (job.state, job.updated_at) == before


@pytest.mark.parametrize("attribute", ["state", "updated_at", "job_id"])
def test_job_cannot_be_mutated_directly(attribute: str) -> None:
    job = running()

    with pytest.raises(FrozenInstanceError):
        setattr(job, attribute, Queued())


def test_transitions_return_a_new_job_and_leave_the_original_intact() -> None:
    job = queued()

    job.start(at(1))

    assert job.status is JobStatus.QUEUED


def test_stage_order_matches_the_contract() -> None:
    assert [stage.value for stage in STAGE_ORDER] == CONTRACT_STAGE_ORDER


@pytest.mark.parametrize(
    ("current", "earlier"),
    [
        (JobStage.RETRIEVING_SOURCES, JobStage.PLANNING),
        (JobStage.FINALIZING, JobStage.FETCHING_MEDIA),
        (JobStage.FINALIZING, JobStage.PLANNING),
    ],
)
def test_stage_never_moves_backwards(current: JobStage, earlier: JobStage) -> None:
    job = running(current, 0.5)

    with pytest.raises(StageRegressionError):
        job.advance(earlier, 0.9, at(10))


def test_advancing_within_the_same_stage_is_allowed() -> None:
    job = running(JobStage.GENERATING_CARDS, 0.5).advance(JobStage.GENERATING_CARDS, 0.6, at(10))

    assert job.stage is JobStage.GENERATING_CARDS
    assert job.progress == Progress(0.6)


def test_a_stage_can_be_skipped_forwards() -> None:
    job = running(JobStage.GENERATING_CARDS, 0.7).advance(JobStage.FINALIZING, 0.9, at(10))

    assert job.stage is JobStage.FINALIZING


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_progress_accepts_the_closed_unit_interval(value: float) -> None:
    assert Progress(value).value == value


@pytest.mark.parametrize("value", [-1e-9, -1.0, 1.0 + 1e-9, 2.0, math.nan, math.inf, -math.inf])
def test_progress_outside_the_unit_interval_is_rejected(value: float) -> None:
    with pytest.raises(InvalidProgressError):
        Progress(value)


def test_progress_rejects_a_lower_value() -> None:
    with pytest.raises(ProgressRegressionError):
        Progress(0.5).advance_to(0.49)


def test_progress_may_stay_equal() -> None:
    assert Progress(0.5).advance_to(0.5) == Progress(0.5)


def test_job_progress_never_decreases_even_when_the_stage_advances() -> None:
    job = running(JobStage.PARSING_SOURCES, 0.4)

    with pytest.raises(ProgressRegressionError):
        job.advance(JobStage.GENERATING_CARDS, 0.3, at(10))

    assert job.progress == Progress(0.4)


def test_progress_is_monotonic_across_a_sequence_of_updates() -> None:
    attempts = [0.1, 0.3, 0.2, 0.3, 0.9, 0.5, 1.0, 0.0]
    job = running()
    observed = [job.progress.value]
    for step, value in enumerate(attempts):
        with suppress(ProgressRegressionError):
            job = job.advance(JobStage.GENERATING_CARDS, value, at(10 + step))
        observed.append(job.progress.value)

    assert observed == sorted(observed)
    assert observed[-1] == 1.0


def test_advance_with_out_of_range_progress_is_rejected() -> None:
    with pytest.raises(InvalidProgressError):
        running().advance(JobStage.PLANNING, 1.5, at(10))


def test_naive_transition_time_is_rejected() -> None:
    with pytest.raises(InvalidTimestampError):
        queued().start(at(1).replace(tzinfo=None))


def test_naive_creation_time_is_rejected() -> None:
    with pytest.raises(InvalidTimestampError):
        GenerationJob.queue(JOB_ID, T0.replace(tzinfo=None))


def test_updated_at_never_moves_backwards_when_the_clock_does() -> None:
    job = queued().start(at(10)).advance(JobStage.PLANNING, 0.1, at(5))

    assert job.updated_at == at(10)


def test_job_id_that_is_not_uuid_v4_is_rejected() -> None:
    with pytest.raises(InvalidJobIdError):
        GenerationJob.queue(UUID(int=1), T0)


def test_rehydrated_job_updated_before_created_is_rejected() -> None:
    with pytest.raises(InvalidTimestampError):
        GenerationJob(
            job_id=JOB_ID,
            created_at=at(10),
            updated_at=at(5),
            state=Running(stage=JobStage.PLANNING, progress=Progress(0.0)),
        )


def test_created_at_is_preserved_across_transitions() -> None:
    assert succeeded().created_at == T0


def test_terminal_statuses_match_the_contract() -> None:
    assert {status for status in JobStatus if status.is_terminal} == {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
