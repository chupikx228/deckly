import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from uuid import UUID

import pytest

from deckly.application.pipeline import progress_within, stage_progress
from deckly.application.ports import JobTransition
from deckly.domain.job import STAGE_ORDER, Cancelled, GenerationJob, JobStage, Succeeded
from deckly.infrastructure.transcription.client import BYTES_PER_SECOND, SAMPLE_RATE
from deckly.infrastructure.transcription.self_hosted import EngineInvocation, SelfHostedTranscriptionClient
from tests.application.test_generate_from_recording import harness_with_recording, queued
from tests.fake_process import fake_program
from tests.fakes import RECORDING_CONTENT, UPLOADED_RECORDING, Harness
from tests.infrastructure.test_recording_transcriber import SENTENCE
from tests.logs import captured_json_logs
from tests.transcription import ScriptedTranscriptionClient, transcriber_with

pytestmark = pytest.mark.anyio

BAND_START = stage_progress(JobStage.PARSING_SOURCES)
BAND_END = stage_progress(JobStage.GENERATING_CARDS)
AUDIO_SECONDS = 12
ENGINE_STEP_SECONDS = 0.15
POLL_SECONDS = 0.01


def inside_band(fraction: float) -> float:
    return BAND_START + (BAND_END - BAND_START) * fraction


def progress_history(harness: Harness, job_id: UUID) -> list[float]:
    return [job.progress.value for job in harness.store.history if job.job_id == job_id]


async def poll_progress(harness: Harness, job_id: UUID, seen: list[float]) -> None:
    while True:
        seen.append((await harness.get(job_id)).progress.value)
        await asyncio.sleep(POLL_SECONDS)


async def test_progress_inside_a_stage_is_a_share_of_that_stage_s_band() -> None:
    assert progress_within(JobStage.PARSING_SOURCES, 0.0) == BAND_START
    assert progress_within(JobStage.PARSING_SOURCES, 1.0) == BAND_END
    assert progress_within(JobStage.PARSING_SOURCES, 0.5) == pytest.approx((BAND_START + BAND_END) / 2)


@pytest.mark.parametrize("fraction", [-5.0, -0.0001, 1.0001, 7.0])
async def test_fraction_outside_zero_to_one_stays_inside_the_stage_band(fraction: float) -> None:
    assert BAND_START <= progress_within(JobStage.PARSING_SOURCES, fraction) <= BAND_END


@pytest.mark.parametrize("stage", STAGE_ORDER)
async def test_every_stage_band_ends_where_the_next_one_begins(stage: JobStage) -> None:
    upper = progress_within(stage, 1.0)
    following = stage.position + 1

    assert upper == pytest.approx(following / len(STAGE_ORDER))
    assert upper >= stage_progress(stage)


async def test_progress_moves_through_the_transcription_band_between_stage_entry_and_the_next_stage() -> None:
    harness = harness_with_recording()
    harness.documents.reports = (0.25, 0.5, 0.75, 1.0)
    job_id = await queued(harness)

    await harness.run(job_id)

    history = progress_history(harness, job_id)
    assert history == sorted(history)
    in_band = [
        job.progress.value
        for job in harness.store.history
        if job.job_id == job_id and job.stage is JobStage.PARSING_SOURCES
    ]
    assert in_band == pytest.approx([BAND_START, *[inside_band(f) for f in (0.25, 0.5, 0.75, 1.0)]])
    assert isinstance(harness.store.jobs[job_id].state, Succeeded)


async def test_polling_during_transcription_sees_progress_move_strictly_between_the_reports() -> None:
    harness = harness_with_recording()
    harness.documents.reports = (0.25, 0.5, 0.75)
    job_id = await queued(harness)
    polled: list[float] = []

    async def observe() -> None:
        polled.append((await harness.get(job_id)).progress.value)

    harness.documents.during["report"] = observe

    await harness.run(job_id)

    assert polled == pytest.approx([inside_band(f) for f in (0.25, 0.5, 0.75)])
    assert all(earlier < later for earlier, later in pairwise(polled))


@pytest.mark.parametrize(
    "reports",
    [
        (0.5, 0.2, 0.9),
        (0.9, 0.9, 0.9),
        (1.0, 0.0),
        (0.5, 0.4, 0.3, 0.2),
        (-1.0, 3.0, -1.0),
    ],
    ids=["dips", "repeats", "full then empty", "only falls", "out of range"],
)
async def test_progress_never_decreases_however_the_engine_reports(reports: Sequence[float]) -> None:
    harness = harness_with_recording()
    harness.documents.reports = tuple(reports)
    job_id = await queued(harness)

    with captured_json_logs() as logs:
        await harness.run(job_id)

    assert logs.named("generation_progress_not_recorded") == []
    history = progress_history(harness, job_id)
    assert history == sorted(history)
    assert all(0.0 <= value <= 1.0 for value in history)
    transcribing = [
        job.progress.value
        for job in harness.store.history
        if job.job_id == job_id and job.stage is JobStage.PARSING_SOURCES
    ]
    assert all(BAND_START <= value <= BAND_END for value in transcribing)


async def test_report_that_does_not_move_progress_forward_writes_nothing_new() -> None:
    harness = harness_with_recording()
    harness.documents.reports = (0.5, 0.5, 0.25)
    job_id = await queued(harness)

    await harness.run(job_id)

    in_band = [
        job for job in harness.store.history if job.job_id == job_id and job.stage is JobStage.PARSING_SOURCES
    ]
    assert [job.progress.value for job in in_band] == pytest.approx(
        [BAND_START, inside_band(0.5), inside_band(0.5), inside_band(0.5)]
    )
    assert len({job.updated_at for job in in_band[1:]}) == 1


async def test_next_stage_never_starts_below_where_transcription_progress_ended() -> None:
    harness = harness_with_recording()
    harness.documents.reports = (1.0,)
    job_id = await queued(harness)

    await harness.run(job_id)

    history = [job for job in harness.store.history if job.job_id == job_id]
    entering_generation = next(job for job in history if job.stage is JobStage.GENERATING_CARDS)
    last_transcribing = [job for job in history if job.stage is JobStage.PARSING_SOURCES][-1]
    assert entering_generation.progress.value >= last_transcribing.progress.value


async def test_progress_that_cannot_be_saved_does_not_fail_a_transcription_that_is_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = harness_with_recording()
    harness.documents.reports = (0.25, 0.5)
    job_id = await queued(harness)
    update = harness.store.update
    failing = {"on": False}

    async def flaky(job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        if failing["on"]:
            message = "job store did not answer"
            raise TimeoutError(message)
        return await update(job_id, transition)

    async def start_failing() -> None:
        failing["on"] = True

    async def recover() -> None:
        failing["on"] = False

    monkeypatch.setattr(harness.store, "update", flaky)
    harness.documents.during["parse"] = start_failing
    harness.documents.during["report"] = recover

    with captured_json_logs() as logs:
        await harness.run(job_id)

    assert isinstance(harness.store.jobs[job_id].state, Succeeded)
    [skipped] = logs.named("generation_progress_not_recorded")
    assert skipped["job_id"] == str(job_id)
    assert skipped["stage"] == JobStage.PARSING_SOURCES


async def test_job_cancelled_during_transcription_stops_at_the_next_progress_report() -> None:
    harness = harness_with_recording()
    harness.documents.reports = (0.25, 0.5, 0.75)
    job_id = await queued(harness)
    reached: list[float] = []
    harness.documents.during["parse"] = lambda: harness.cancel(job_id)

    async def note() -> None:
        reached.append((await harness.get(job_id)).progress.value)

    harness.documents.during["report"] = note

    with captured_json_logs() as logs:
        await harness.run(job_id)

    state = harness.store.jobs[job_id].state
    assert isinstance(state, Cancelled)
    assert state.stage is JobStage.PARSING_SOURCES
    assert state.progress.value == BAND_START
    assert reached == []
    assert harness.providers.generated_for == []
    assert logs.named("generation_stopped")


async def test_recording_job_polled_while_a_slow_engine_transcribes_sees_progress_advance(
    tmp_path: Path,
) -> None:
    harness = harness_with_recording()
    harness.documents.keep(UPLOADED_RECORDING, RECORDING_CONTENT)
    samples = AUDIO_SECONDS * SAMPLE_RATE
    steps = [
        {"delay": ENGINE_STEP_SECONDS, "text": f"progress {samples * share // 4}\n"} for share in (1, 2, 3, 4)
    ]
    answer = {
        "language": "en",
        "segments": [{"start_ms": 0, "end_ms": 10_000, "text": f"{SENTENCE} (0)"}],
    }
    engine = fake_program(tmp_path, "engine", {"steps": steps, "stdout_text": json.dumps(answer)})
    ffmpeg = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": AUDIO_SECONDS * BYTES_PER_SECOND})
    real = replace(
        transcriber_with(ffmpeg, ScriptedTranscriptionClient()),
        client=SelfHostedTranscriptionClient(
            invocation=EngineInvocation(model_directory=tmp_path, cpu_threads=1, command=(str(engine.path),)),
            deadline_seconds=30,
        ),
    )
    run = replace(harness.run, documents=replace(harness.run.documents, parser=real))
    job_id = await queued(harness)
    polled: list[float] = []

    polling = asyncio.create_task(poll_progress(harness, job_id, polled))
    try:
        await run(job_id)
    finally:
        polling.cancel()

    assert isinstance(harness.store.jobs[job_id].state, Succeeded)
    assert polled == sorted(polled)
    moving = sorted({value for value in polled if BAND_START < value < BAND_END})
    assert moving == pytest.approx([inside_band(share / 4) for share in (1, 2, 3)])
    assert len([value for value in polled if BAND_START <= value <= BAND_END]) > len(moving)
