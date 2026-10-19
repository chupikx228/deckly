import asyncio
import json
import time
from itertools import pairwise
from pathlib import Path

import pytest

from deckly.infrastructure.transcription.client import (
    DecodedAudio,
    Transcript,
    TranscriptionResponseError,
    TranscriptionUnavailableError,
    TranscriptSegment,
)
from deckly.infrastructure.transcription.self_hosted import (
    ENGINE_COMMAND,
    ENGINE_MODULE,
    EngineInvocation,
    SelfHostedTranscriptionClient,
)
from tests.fake_process import FakeProgram, fake_program
from tests.infrastructure.test_recording_url_fetcher import (
    is_gone,
    same_directory,
    wait_for_pid,
    wait_until_gone,
)

pytestmark = pytest.mark.anyio

MODEL = Path("/models/faster-whisper-small")
THREADS = 3
DEADLINE_SECONDS = 10
AUDIO = DecodedAudio(path=Path("/work/audio.pcm"), samples=48_000, truncated=False)
ANSWER = {
    "language": "ru",
    "segments": [
        {"start_ms": 0, "end_ms": 2880, "text": "Перебираться в Жиздренский"},
        {"start_ms": 2880, "end_ms": 5320, "text": "между породой людей"},
    ],
}


def client(
    program: FakeProgram, *, deadline_seconds: float = DEADLINE_SECONDS
) -> SelfHostedTranscriptionClient:
    return SelfHostedTranscriptionClient(
        invocation=EngineInvocation(model_directory=MODEL, cpu_threads=THREADS, command=(str(program.path),)),
        deadline_seconds=deadline_seconds,
    )


def test_engine_runs_in_isolated_mode_as_a_module_of_this_package() -> None:
    assert ENGINE_COMMAND[1:] == ("-I", "-m", ENGINE_MODULE)
    assert ENGINE_MODULE == "deckly.infrastructure.transcription.engine"


async def test_engine_answer_becomes_the_transcript(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "engine", {"stdout_text": json.dumps(ANSWER)})

    transcript = await client(program).transcribe(AUDIO)

    assert transcript == Transcript(
        segments=(
            TranscriptSegment(start_ms=0, end_ms=2880, text="Перебираться в Жиздренский"),
            TranscriptSegment(start_ms=2880, end_ms=5320, text="между породой людей"),
        ),
        language="ru",
    )


async def test_engine_is_told_the_model_threads_and_the_decoded_sample_count(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "engine", {"stdout_text": json.dumps(ANSWER)})

    await client(program).transcribe(AUDIO)

    [invocation] = program.invocations
    assert invocation.args == [
        "--model",
        str(MODEL),
        "--threads",
        str(THREADS),
        "--samples",
        str(AUDIO.samples),
        "--",
        str(AUDIO.path),
    ]


async def test_engine_runs_offline_in_its_own_scratch_directory_with_no_path(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "engine", {"stdout_text": json.dumps(ANSWER)})

    await client(program).transcribe(AUDIO)

    [invocation] = program.invocations
    assert invocation.env["HF_HUB_OFFLINE"] == "1"
    assert invocation.env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert invocation.env["PATH"] == ""
    assert invocation.env["OMP_NUM_THREADS"] == str(THREADS)
    assert invocation.env["HOME"] == invocation.env["TMPDIR"]
    assert await asyncio.to_thread(same_directory, invocation.env["HOME"], invocation.cwd)
    assert await asyncio.to_thread(is_gone, invocation.cwd)


async def test_engine_that_fails_is_unavailable_and_names_its_last_error_line(tmp_path: Path) -> None:
    program = fake_program(
        tmp_path, "engine", {"stderr": "loading\nRuntimeError: out of memory\n", "exit": 1}
    )

    with pytest.raises(TranscriptionUnavailableError, match="exited 1: RuntimeError: out of memory"):
        await client(program).transcribe(AUDIO)


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({"segments": []}),
        json.dumps({**ANSWER, "words": []}),
        json.dumps({"language": None, "segments": [{"start_ms": -1, "end_ms": 1, "text": "x"}]}),
    ],
    ids=["not json", "no language", "unknown field", "negative timestamp"],
)
async def test_engine_answer_in_an_unexpected_shape_is_a_response_error(tmp_path: Path, answer: str) -> None:
    program = fake_program(tmp_path, "engine", {"stdout_text": answer})

    with pytest.raises(TranscriptionResponseError, match="unexpected shape"):
        await client(program).transcribe(AUDIO)


async def test_engine_that_writes_without_end_is_stopped_at_the_output_cap(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    program = fake_program(tmp_path, "engine", {"stdout_forever": True, "child_pid_file": str(pid_file)})

    with pytest.raises(TranscriptionResponseError, match="wrote more than"):
        await client(program).transcribe(AUDIO)

    assert await asyncio.to_thread(wait_until_gone, wait_for_pid(pid_file))


async def test_engine_over_its_deadline_is_unavailable_and_killed_with_everything_it_started(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    program = fake_program(tmp_path, "engine", {"sleep": 60, "child_pid_file": str(pid_file)})
    started = time.monotonic()

    with pytest.raises(TranscriptionUnavailableError, match=r"did not finish within 0\.5 seconds"):
        await client(program, deadline_seconds=0.5).transcribe(AUDIO)

    assert time.monotonic() - started < DEADLINE_SECONDS
    assert await asyncio.to_thread(wait_until_gone, wait_for_pid(pid_file))
    [invocation] = program.invocations
    assert await asyncio.to_thread(is_gone, invocation.cwd)


async def test_cancelled_transcription_kills_the_engine_so_it_stops_using_the_cpu(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    program = fake_program(tmp_path, "engine", {"sleep": 60, "child_pid_file": str(pid_file)})
    task = asyncio.ensure_future(client(program).transcribe(AUDIO))
    child = await asyncio.to_thread(wait_for_pid, pid_file)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(wait_until_gone, child)


async def test_closing_the_self_hosted_client_holds_nothing_open(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "engine", {})

    await client(program).aclose()

    assert program.invocations == []


def progress(samples: int) -> str:
    return f"progress {samples}\n"


class Reports:
    def __init__(self) -> None:
        self.fractions: list[float] = []
        self.times: list[float] = []

    async def __call__(self, fraction: float) -> None:
        self.fractions.append(fraction)
        self.times.append(time.monotonic())


async def test_engine_progress_lines_are_reported_as_fractions_of_the_audio_and_are_not_part_of_the_answer(
    tmp_path: Path,
) -> None:
    steps = [{"delay": 0, "text": progress(samples)} for samples in (12_000, 24_000, 48_000)]
    program = fake_program(tmp_path, "engine", {"steps": steps, "stdout_text": json.dumps(ANSWER)})
    reports = Reports()

    transcript = await client(program).transcribe(AUDIO, reports)

    assert reports.fractions == [0.25, 0.5, 1.0]
    assert [segment.text for segment in transcript.segments] == [
        "Перебираться в Жиздренский",
        "между породой людей",
    ]


async def test_progress_arrives_while_the_engine_is_still_working_not_when_it_exits(tmp_path: Path) -> None:
    delay = 0.15
    steps = [
        {"delay": delay, "text": progress(16_000)},
        {"delay": delay, "text": progress(32_000)},
        {"delay": delay, "text": progress(48_000)},
    ]
    program = fake_program(tmp_path, "engine", {"steps": steps, "stdout_text": json.dumps(ANSWER)})
    reports = Reports()

    await client(program).transcribe(AUDIO, reports)

    gaps = [later - earlier for earlier, later in pairwise(reports.times)]
    assert len(gaps) == 2
    assert all(gap >= delay * 0.8 for gap in gaps)


async def test_progress_beyond_the_decoded_audio_is_capped_at_complete(tmp_path: Path) -> None:
    steps = [{"delay": 0, "text": progress(AUDIO.samples * 5)}]
    program = fake_program(tmp_path, "engine", {"steps": steps, "stdout_text": json.dumps(ANSWER)})
    reports = Reports()

    await client(program).transcribe(AUDIO, reports)

    assert reports.fractions == [1.0]


async def test_progress_lines_split_across_reads_are_still_whole_reports(tmp_path: Path) -> None:
    steps = [
        {"delay": 0, "text": "prog"},
        {"delay": 0.05, "text": "ress 2400"},
        {"delay": 0.05, "text": "0\n"},
    ]
    program = fake_program(tmp_path, "engine", {"steps": steps, "stdout_text": json.dumps(ANSWER)})
    reports = Reports()

    await client(program).transcribe(AUDIO, reports)

    assert reports.fractions == [0.5]


@pytest.mark.parametrize("bad", ["progress soon\n", "progress -1\n", "progress \n"])
async def test_malformed_progress_line_is_an_unexpected_answer_not_silently_dropped(
    tmp_path: Path, bad: str
) -> None:
    program = fake_program(
        tmp_path, "engine", {"steps": [{"delay": 0, "text": bad}], "stdout_text": json.dumps(ANSWER)}
    )

    with pytest.raises(TranscriptionResponseError, match="unexpected shape"):
        await client(program).transcribe(AUDIO)


async def test_engine_that_exits_failing_after_reporting_progress_is_still_unavailable(
    tmp_path: Path,
) -> None:
    program = fake_program(
        tmp_path,
        "engine",
        {
            "steps": [{"delay": 0, "text": progress(12_000)}],
            "stderr": "RuntimeError: out of memory\n",
            "exit": 1,
        },
    )
    reports = Reports()

    with pytest.raises(TranscriptionUnavailableError, match="exited 1: RuntimeError: out of memory"):
        await client(program).transcribe(AUDIO, reports)

    assert reports.fractions == [0.25]


async def test_progress_report_that_raises_stops_the_engine_and_everything_it_started(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    program = fake_program(
        tmp_path,
        "engine",
        {"steps": [{"delay": 0, "text": progress(12_000)}], "sleep": 60, "child_pid_file": str(pid_file)},
    )

    class StoppedError(Exception):
        pass

    async def report(fraction: float) -> None:
        del fraction
        raise StoppedError

    started = time.monotonic()
    with pytest.raises(StoppedError):
        await client(program).transcribe(AUDIO, report)

    assert time.monotonic() - started < DEADLINE_SECONDS
    assert await asyncio.to_thread(wait_until_gone, wait_for_pid(pid_file))


async def test_slow_progress_report_counts_against_the_engine_deadline(tmp_path: Path) -> None:
    program = fake_program(
        tmp_path,
        "engine",
        {"steps": [{"delay": 0, "text": progress(12_000)}], "stdout_text": json.dumps(ANSWER)},
    )

    async def slow(fraction: float) -> None:
        del fraction
        await asyncio.sleep(5)

    with pytest.raises(TranscriptionUnavailableError, match="did not finish"):
        await client(program, deadline_seconds=0.3).transcribe(AUDIO, slow)
