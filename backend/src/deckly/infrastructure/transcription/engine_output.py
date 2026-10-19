from pydantic import BaseModel, ConfigDict, NonNegativeInt

from deckly.infrastructure.transcription.client import Transcript, TranscriptSegment

PROGRESS_PREFIX = "progress "
PROGRESS_PREFIX_BYTES = PROGRESS_PREFIX.encode()
MAX_PROGRESS_DIGITS = 18


def progress_line(processed_samples: int) -> str:
    return f"{PROGRESS_PREFIX}{processed_samples}\n"


def processed_samples_in(line: bytes) -> int | None:
    if not line.startswith(PROGRESS_PREFIX_BYTES):
        return None
    digits = line[len(PROGRESS_PREFIX_BYTES) :].strip()
    if not digits.isdigit() or len(digits) > MAX_PROGRESS_DIGITS:
        return None
    return int(digits)


class EngineSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    text: str


class EngineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str | None
    segments: tuple[EngineSegment, ...]

    def transcript(self) -> Transcript:
        return Transcript(
            segments=tuple(
                TranscriptSegment(start_ms=segment.start_ms, end_ms=segment.end_ms, text=segment.text)
                for segment in self.segments
            ),
            language=self.language,
        )
