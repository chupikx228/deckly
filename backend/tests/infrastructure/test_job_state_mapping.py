from collections.abc import Callable

import pytest

from deckly.domain.job import FailureCode, GenerationJob, JobStage
from deckly.infrastructure.job_store import CorruptStoredJobError, StoredState, restore_state, store_state
from deckly.infrastructure.stored_result import dump_result
from tests.domain.builders import FULL_RESULT, JOB_ID, T0, at, basic_note, result_with


def queued() -> GenerationJob:
    return GenerationJob.queue(JOB_ID, T0)


def running() -> GenerationJob:
    return queued().start(at(1)).advance(JobStage.GENERATING_CARDS, 0.62, at(2))


STORABLE_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "queued": queued,
    "running": running,
    "succeeded with every note type": lambda: running().succeed(FULL_RESULT, at(3)),
    "succeeded with a bare deck and no notes": lambda: running().succeed(result_with(), at(3)),
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


def test_only_a_succeeded_state_carries_a_result() -> None:
    stored = {name: store_state(make_job().state) for name, make_job in STORABLE_JOBS.items()}

    assert {name for name, state in stored.items() if state.result is not None} == {
        "succeeded with every note type",
        "succeeded with a bare deck and no notes",
    }


def test_stored_state_keeps_the_result_out_of_its_repr() -> None:
    stored = store_state(running().succeed(FULL_RESULT, at(3)).state)

    assert FULL_RESULT.deck.title not in repr(stored)


BASIC_RESULT = dump_result(result_with(basic_note(1)))


@pytest.mark.parametrize(
    "stored",
    [
        StoredState("paused", None, 0.0, None, None),
        StoredState("queued", "planning", 0.0, None, None),
        StoredState("running", None, 0.5, None, None),
        StoredState("running", "dreaming", 0.5, None, None),
        StoredState("failed", "planning", 0.5, None, None),
        StoredState("failed", "planning", 0.5, "model returned garbage", None),
        StoredState("succeeded", None, 1.0, None, None),
        StoredState("succeeded", "finalizing", 1.0, None, BASIC_RESULT),
        StoredState("succeeded", None, 1.0, None, {"deck": {"title": "Road signs"}}),
        StoredState("succeeded", None, 1.0, None, {**BASIC_RESULT, "notes": [{"note_type": "basic"}]}),
    ],
    ids=[
        "unknown status",
        "queued with a stage",
        "running without a stage",
        "unknown stage",
        "failed without a code",
        "failed with a free-text reason instead of a code",
        "succeeded without a result",
        "succeeded with a stage",
        "succeeded with a result that has no notes",
        "succeeded with a malformed note",
    ],
)
def test_corrupt_stored_state_is_rejected(stored: StoredState) -> None:
    with pytest.raises(CorruptStoredJobError):
        restore_state(stored)
