from dataclasses import dataclass, field, replace
from pathlib import Path

from deckly.domain.document import Document, DocumentFormat
from deckly.infrastructure.transcription.client import (
    MILLISECONDS_PER_SECOND,
    DecodedAudio,
    Transcript,
    TranscriptSegment,
)
from deckly.infrastructure.transcription.decoding import FfmpegAudioDecoder
from deckly.infrastructure.transcription.transcriber import RecordingTranscriber
from tests.documents import document
from tests.fake_process import FakeProgram
from tests.fakes import fresh_metrics

RECORDING_FILENAME = "lecture-3.m4a"
TEST_DECODE_CAP_SECONDS = 60
TEST_DECODE_DEADLINE_SECONDS = 10
TEST_MAX_CHARACTERS = 4000


def recording(
    number: int = 1, *, document_format: DocumentFormat = DocumentFormat.M4A, seconds: float = 12.0
) -> Document:
    return replace(
        document(number),
        filename=RECORDING_FILENAME,
        document_format=document_format,
        duration_seconds=seconds,
    )


def segment(start_seconds: float, end_seconds: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        start_ms=round(start_seconds * MILLISECONDS_PER_SECOND),
        end_ms=round(end_seconds * MILLISECONDS_PER_SECOND),
        text=text,
    )


@dataclass(frozen=True, slots=True)
class Received:
    audio: DecodedAudio
    bytes_on_disk: int


@dataclass
class ScriptedTranscriptionClient:
    transcript: Transcript = field(default_factory=lambda: Transcript(segments=(), language=None))
    failure: Exception | None = None
    received: list[Received] = field(default_factory=list)
    closed: bool = False

    async def transcribe(self, audio: DecodedAudio) -> Transcript:
        self.received.append(Received(audio=audio, bytes_on_disk=audio.path.stat().st_size))
        if self.failure is not None:
            raise self.failure
        return self.transcript

    async def aclose(self) -> None:
        self.closed = True


def transcriber_with(
    ffmpeg: FakeProgram | Path,
    client: ScriptedTranscriptionClient,
    *,
    max_characters: int = TEST_MAX_CHARACTERS,
    max_seconds: int = TEST_DECODE_CAP_SECONDS,
) -> RecordingTranscriber:
    binary = ffmpeg.path if isinstance(ffmpeg, FakeProgram) else ffmpeg
    return RecordingTranscriber(
        decoder=FfmpegAudioDecoder(
            binary=binary, max_seconds=max_seconds, deadline_seconds=TEST_DECODE_DEADLINE_SECONDS
        ),
        client=client,
        max_characters=max_characters,
        metrics=fresh_metrics(),
    )
