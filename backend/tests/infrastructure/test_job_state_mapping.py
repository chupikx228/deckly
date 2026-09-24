from collections.abc import Callable

import pytest

from deckly.domain.job import FailureCode, GenerationJob, JobStage
from deckly.infrastructure.job_store import (
    CorruptStoredJobError,
    StoredState,
    UnstorableJobStateError,
    restore_state,
    store_state,
)
from tests.domain.builders import JOB_ID, T0, at, basic_note, result_with


def queued() -> GenerationJob:
    return GenerationJob.queue(JOB_ID, T0)


def running() -> GenerationJob:
    return queued().start(at(1)).advance(JobStage.GENERATING_CARDS, 0.62, at(2))


STORABLE_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "queued": queued,
    "running": running,
    "failed with no valid content": lambda: running().fail(FailureCode.NO_VALID_CONTENT, at(3)),
    "failed with the provider unavailable": lambda: running().fail(FailureCode.PROVIDER_UNAVAILABLE, at(3)),
    "cancelled while running": lambda: running().cancel(at(3)),
    "cancelled while queued": lambda: queued().cancel(at(3)),
    "failed while queued": lambda: queued().fail(FailureCode.GENERATION_FAILED, at(3)),
}


@pytest.mark.parametrize("make_job", STORABLE_JOBS.values(), ids=STORABLE_JOBS.keys())
def test_every_storable_state_survives_a_round_trip(make_job: Callable[[], GenerationJob]) -> None:
    state = make_job().state

    assert restore_state(store_state(state)) == state


def test_succeeded_state_is_refused_rather_than_silently_losing_the_result() -> None:
    succeeded = running().succeed(result_with(basic_note(1)), at(3))

    with pytest.raises(UnstorableJobStateError):
        store_state(succeeded.state)


@pytest.mark.parametrize(
    "stored",
    [
        StoredState("paused", None, 0.0, None),
        StoredState("queued", "planning", 0.0, None),
        StoredState("running", None, 0.5, None),
        StoredState("running", "dreaming", 0.5, None),
        StoredState("failed", "planning", 0.5, None),
        StoredState("failed", "planning", 0.5, "model returned garbage"),
        StoredState("succeeded", None, 1.0, None),
    ],
    ids=[
        "unknown status",
        "queued with a stage",
        "running without a stage",
        "unknown stage",
        "failed without a reason",
        "failed with a free-text reason instead of a code",
        "succeeded without a result",
    ],
)
def test_corrupt_stored_state_is_rejected(stored: StoredState) -> None:
    with pytest.raises(CorruptStoredJobError):
        restore_state(stored)
