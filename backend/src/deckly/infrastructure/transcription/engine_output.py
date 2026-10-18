from pydantic import BaseModel, ConfigDict, NonNegativeInt

from deckly.infrastructure.transcription.client import Transcript, TranscriptSegment


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
