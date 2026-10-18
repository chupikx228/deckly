from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from deckly.infrastructure.resilience import TransientError

SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH_BYTES
MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True, slots=True)
class DecodedAudio:
    path: Path
    samples: int
    truncated: bool

    @property
    def seconds(self) -> float:
        return self.samples / SAMPLE_RATE


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class Transcript:
    segments: tuple[TranscriptSegment, ...]
    language: str | None


class TranscriptionClient(Protocol):
    async def transcribe(self, audio: DecodedAudio) -> Transcript: ...

    async def aclose(self) -> None: ...


class TranscriptionError(Exception):
    pass


class TranscriptionUnavailableError(TranscriptionError, TransientError):
    pass


class TranscriptionResponseError(TranscriptionError):
    pass
