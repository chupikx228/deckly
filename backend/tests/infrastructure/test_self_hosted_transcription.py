import asyncio
import json
import time
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
