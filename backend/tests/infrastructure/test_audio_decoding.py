import asyncio
import time
from pathlib import Path

import pytest

from deckly.application.exceptions import DocumentHasNoTextError, UnreadableDocumentError
from deckly.domain.document import DocumentFormat
from deckly.infrastructure.transcription.client import BYTES_PER_SECOND, SAMPLE_RATE
from deckly.infrastructure.transcription.decoding import FfmpegAudioDecoder
from tests.fake_process import FakeProgram, fake_program
from tests.infrastructure.test_recording_url_fetcher import wait_for_pid, wait_until_gone

pytestmark = pytest.mark.anyio

CAP_SECONDS = 2
DEADLINE_SECONDS = 10


def decoder(program: FakeProgram, *, deadline_seconds: float = DEADLINE_SECONDS) -> FfmpegAudioDecoder:
    return FfmpegAudioDecoder(binary=program.path, max_seconds=CAP_SECONDS, deadline_seconds=deadline_seconds)


def recording(tmp_path: Path) -> Path:
    source = tmp_path / "work" / "recording"
    source.parent.mkdir()
    source.write_bytes(b"not decoded by the fake")
    return source


@pytest.mark.parametrize(
    ("document_format", "demuxer"),
    [
        (DocumentFormat.MP3, "mp3"),
        (DocumentFormat.WAV, "wav"),
        (DocumentFormat.M4A, "mov"),
        (DocumentFormat.MP4, "mov"),
        (DocumentFormat.MOV, "mov"),
    ],
)
def test_ffmpeg_is_run_with_the_demuxer_of_the_sniffed_type_and_every_hardening_flag(
    document_format: DocumentFormat, demuxer: str
) -> None:
    arguments = FfmpegAudioDecoder(Path("/usr/bin/ffmpeg"), CAP_SECONDS, DEADLINE_SECONDS).arguments(
        Path("/work/recording"), document_format
    )

    assert arguments[0] == "/usr/bin/ffmpeg"
    assert arguments[arguments.index("-f") + 1] == demuxer
    assert arguments[arguments.index("-protocol_whitelist") + 1] == "file"
    assert arguments[arguments.index("-i") + 1] == "/work/recording"
    assert "-t" not in arguments
    assert {"-nostdin", "-vn", "-sn", "-dn"} <= set(arguments)
    assert arguments[arguments.index("-map_metadata") + 1] == "-1"
    assert arguments[arguments.index("-ac") + 1] == "1"
    assert arguments[arguments.index("-ar") + 1] == str(SAMPLE_RATE)
    assert arguments[arguments.index("-c:a") + 1] == "pcm_s16le"
    assert arguments[-5:] == ["-f", "s16le", "-threads", "1", "pipe:1"]


async def test_document_that_is_no_recording_is_refused_without_running_ffmpeg(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": 100})

    with pytest.raises(UnreadableDocumentError, match="no audio decoder"):
        await decoder(program).decode(recording(tmp_path), DocumentFormat.PDF, tmp_path / "audio.pcm")

    assert program.invocations == []


async def test_audio_under_the_cap_is_kept_whole_and_not_marked_truncated(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": BYTES_PER_SECOND // 2})
    target = tmp_path / "audio.pcm"

    audio = await decoder(program).decode(recording(tmp_path), DocumentFormat.MP3, target)

    assert audio.samples == SAMPLE_RATE // 2
    assert audio.seconds == pytest.approx(0.5)
    assert audio.truncated is False
    assert audio.path == target
    assert target.stat().st_size == BYTES_PER_SECOND // 2


async def test_audio_that_ends_exactly_at_the_cap_is_not_marked_truncated(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": CAP_SECONDS * BYTES_PER_SECOND})

    audio = await decoder(program).decode(recording(tmp_path), DocumentFormat.WAV, tmp_path / "audio.pcm")

    assert audio.samples == CAP_SECONDS * SAMPLE_RATE
    assert audio.truncated is False


async def test_decoder_that_never_stops_is_cut_at_exactly_the_cap_and_killed(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    program = fake_program(tmp_path, "ffmpeg", {"stdout_forever": True, "child_pid_file": str(pid_file)})
    target = tmp_path / "audio.pcm"
    started = time.monotonic()

    audio = await decoder(program).decode(recording(tmp_path), DocumentFormat.M4A, target)

    assert time.monotonic() - started < DEADLINE_SECONDS
    assert audio.truncated is True
    assert audio.samples == CAP_SECONDS * SAMPLE_RATE
    assert target.stat().st_size == CAP_SECONDS * BYTES_PER_SECOND
    assert await asyncio.to_thread(wait_until_gone, wait_for_pid(pid_file))


async def test_failed_decode_is_unreadable_and_names_ffmpegs_last_error_line(tmp_path: Path) -> None:
    program = fake_program(
        tmp_path, "ffmpeg", {"stderr": "first\nmoov atom not found\n", "exit": 1, "stdout_bytes": 64}
    )

    with pytest.raises(UnreadableDocumentError, match="moov atom not found"):
        await decoder(program).decode(recording(tmp_path), DocumentFormat.MP4, tmp_path / "audio.pcm")


async def test_recording_that_decodes_to_no_audio_has_no_text(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "ffmpeg", {})

    with pytest.raises(DocumentHasNoTextError, match="no audio"):
        await decoder(program).decode(recording(tmp_path), DocumentFormat.MOV, tmp_path / "audio.pcm")


async def test_trailing_half_sample_is_not_counted(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": 2 * 100 + 1})

    audio = await decoder(program).decode(recording(tmp_path), DocumentFormat.MP3, tmp_path / "audio.pcm")

    assert audio.samples == 100


async def test_decode_over_its_deadline_is_unreadable_and_kills_ffmpeg_and_its_children(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    program = fake_program(tmp_path, "ffmpeg", {"sleep": 60, "child_pid_file": str(pid_file)})
    started = time.monotonic()

    with pytest.raises(UnreadableDocumentError, match=r"longer than 0\.5 seconds"):
        await decoder(program, deadline_seconds=0.5).decode(
            recording(tmp_path), DocumentFormat.MP3, tmp_path / "audio.pcm"
        )

    assert time.monotonic() - started < DEADLINE_SECONDS
    assert await asyncio.to_thread(wait_until_gone, wait_for_pid(pid_file))


async def test_cancelled_decode_kills_ffmpeg_and_its_children(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    program = fake_program(tmp_path, "ffmpeg", {"sleep": 60, "child_pid_file": str(pid_file)})
    task = asyncio.ensure_future(
        decoder(program).decode(recording(tmp_path), DocumentFormat.MP3, tmp_path / "audio.pcm")
    )
    child = await asyncio.to_thread(wait_for_pid, pid_file)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(wait_until_gone, child)


async def test_ffmpeg_runs_beside_the_recording_with_no_path_to_find_other_programs(tmp_path: Path) -> None:
    program = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": 64})
    source = recording(tmp_path)

    await decoder(program).decode(source, DocumentFormat.MP3, tmp_path / "audio.pcm")

    [invocation] = program.invocations
    assert invocation.cwd == str(source.parent)
    assert invocation.env["PATH"] == ""
    assert set(invocation.env) <= {"PATH", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}


async def test_recording_named_by_a_relative_path_is_found_although_ffmpeg_runs_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": 64})
    source = recording(tmp_path)
    monkeypatch.chdir(tmp_path)

    await decoder(program).decode(source.relative_to(tmp_path), DocumentFormat.MP3, tmp_path / "audio.pcm")

    [invocation] = program.invocations
    assert invocation.args[invocation.args.index("-i") + 1] == str(source)
