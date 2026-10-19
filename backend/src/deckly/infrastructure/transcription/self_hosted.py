import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from deckly.application.progress import ProgressReport, no_progress
from deckly.infrastructure.transcription.client import (
    DecodedAudio,
    Transcript,
    TranscriptionResponseError,
    TranscriptionUnavailableError,
)
from deckly.infrastructure.transcription.engine_output import EngineOutput, processed_samples_in
from deckly.infrastructure.transcription.process import Stop, supervise

ENGINE_MODULE = "deckly.infrastructure.transcription.engine"
ENGINE_COMMAND = (sys.executable, "-I", "-m", ENGINE_MODULE)
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
WORKDIR_PREFIX = "deckly-transcription-engine-"


@dataclass(frozen=True, slots=True)
class EngineInvocation:
    model_directory: Path
    cpu_threads: int
    command: tuple[str, ...] = ENGINE_COMMAND

    def arguments(self, audio: DecodedAudio) -> list[str]:
        return [
            *self.command,
            "--model",
            str(self.model_directory),
            "--threads",
            str(self.cpu_threads),
            "--samples",
            str(audio.samples),
            "--",
            str(audio.path),
        ]

    def environment(self, workdir: Path) -> dict[str, str]:
        return {
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "PATH": "",
            "LC_ALL": "C.UTF-8",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "OMP_NUM_THREADS": str(self.cpu_threads),
        }


type ProcessedSamples = Callable[[int], Awaitable[None]]

NEWLINE = b"\n"


def take_lines(pending: bytearray) -> list[bytes]:
    lines: list[bytes] = []
    while (end := pending.find(NEWLINE)) >= 0:
        lines.append(bytes(pending[: end + 1]))
        del pending[: end + 1]
    return lines


async def read_capped(source: asyncio.StreamReader, stop: Stop, processed: ProcessedSamples) -> bytes | None:
    output = bytearray()
    pending = bytearray()
    while chunk := await source.read(READ_CHUNK_BYTES):
        pending += chunk
        for line in take_lines(pending):
            samples = processed_samples_in(line)
            if samples is None:
                output += line
            else:
                await processed(samples)
        if len(output) + len(pending) > MAX_OUTPUT_BYTES:
            stop()
            return None
    return bytes(output + pending)


def completed_fraction(processed: int, total: int) -> float:
    return min(1.0, processed / max(1, total))


def transcript_from(raw: bytes) -> Transcript:
    try:
        return EngineOutput.model_validate_json(raw).transcript()
    except ValidationError as error:
        message = "the transcription engine answered in an unexpected shape"
        raise TranscriptionResponseError(message) from error


@dataclass(frozen=True, slots=True)
class SelfHostedTranscriptionClient:
    invocation: EngineInvocation
    deadline_seconds: float

    async def transcribe(self, audio: DecodedAudio, report: ProgressReport = no_progress) -> Transcript:
        async def processed(samples: int) -> None:
            await report(completed_fraction(samples, audio.samples))

        with TemporaryDirectory(prefix=WORKDIR_PREFIX) as directory:
            workdir = Path(directory)
            try:
                async with asyncio.timeout(self.deadline_seconds):
                    finished = await supervise(
                        self.invocation.arguments(audio),
                        environment=self.invocation.environment(workdir),
                        workdir=workdir,
                        consume=lambda source, stop: read_capped(source, stop, processed),
                    )
            except TimeoutError as error:
                message = f"the transcription engine did not finish within {self.deadline_seconds} seconds"
                raise TranscriptionUnavailableError(message) from error
        if finished.output is None:
            message = f"the transcription engine wrote more than {MAX_OUTPUT_BYTES} bytes"
            raise TranscriptionResponseError(message)
        if finished.returncode != 0:
            message = f"the transcription engine exited {finished.returncode}: {finished.last_error_line}"
            raise TranscriptionUnavailableError(message)
        return transcript_from(finished.output)

    async def aclose(self) -> None:
        return
