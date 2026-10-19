import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from deckly.application.exceptions import (
    DocumentHasNoTextError,
    UnreadableDocumentError,
    UpstreamUnavailableError,
)
from deckly.application.pipeline import failure_level
from deckly.application.ports import SourceMaterial
from deckly.application.progress import ProgressReport, no_progress
from deckly.domain.document import Document, is_recording
from deckly.domain.source import TranscriptSource
from deckly.domain.text import visible_length
from deckly.infrastructure.documents.extraction import MonotonicClock
from deckly.infrastructure.documents.parser import usable_text
from deckly.infrastructure.observability.metrics import Metrics
from deckly.infrastructure.search.cleaning import clean_title
from deckly.infrastructure.search.parser import MIN_VISIBLE_CHARACTERS, RAW_WINDOW_MULTIPLIER
from deckly.infrastructure.transcription.cache import TranscriptCache
from deckly.infrastructure.transcription.client import (
    DecodedAudio,
    Transcript,
    TranscriptionClient,
    TranscriptionError,
    TranscriptSegment,
)
from deckly.infrastructure.transcription.decoding import FfmpegAudioDecoder

logger = logging.getLogger(__name__)

PASSAGE_MILLISECONDS = 60_000
TRANSCRIPTION_RETRY_AFTER_SECONDS = 30
WORKDIR_PREFIX = "deckly-transcription-"
RECORDING_FILE = "recording"
AUDIO_FILE = "audio.pcm"
PASSAGE_SEPARATOR = " "


@dataclass(slots=True)
class Passage:
    start_ms: int
    end_ms: int
    texts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return PASSAGE_SEPARATOR.join(self.texts)

    @property
    def span_ms(self) -> int:
        return self.end_ms - self.start_ms


def passages(segments: Iterable[TranscriptSegment], passage_ms: int) -> list[Passage]:
    grouped: list[Passage] = []
    current: Passage | None = None
    for segment in segments:
        if current is None:
            current = Passage(start_ms=segment.start_ms, end_ms=segment.end_ms)
        current.texts.append(segment.text)
        current.end_ms = max(current.end_ms, segment.end_ms)
        if current.span_ms >= passage_ms:
            grouped.append(current)
            current = None
    if current is not None:
        grouped.append(current)
    return grouped


@dataclass(frozen=True, slots=True)
class RecordingTranscriber:
    decoder: FfmpegAudioDecoder
    client: TranscriptionClient
    cache: TranscriptCache
    max_characters: int
    metrics: Metrics
    passage_ms: int = PASSAGE_MILLISECONDS
    clock: MonotonicClock = time.monotonic

    async def parse(
        self, document: Document, content: bytes, report: ProgressReport = no_progress
    ) -> tuple[SourceMaterial, ...]:
        if not is_recording(document.document_format):
            message = f"documents of type {document.document_format} are not recordings to transcribe"
            raise UnreadableDocumentError(message)
        cached = await self._recall(document)
        if cached is not None:
            return self._material(document, cached)
        with TemporaryDirectory(prefix=WORKDIR_PREFIX) as directory:
            workdir = Path(directory)
            source = workdir / RECORDING_FILE
            await asyncio.to_thread(source.write_bytes, content)
            audio = await self.decoder.decode(source, document.document_format, workdir / AUDIO_FILE)
            self._decoded(document, audio)
            transcript = await self._transcribe(document, audio, report)
        if not audio.truncated:
            await self._remember(document, transcript)
        return self._material(document, transcript)

    async def _recall(self, document: Document) -> Transcript | None:
        try:
            return await self.cache.get(document.document_id)
        except Exception as error:
            logger.log(
                failure_level(error),
                "transcript_cache_read_failed",
                exc_info=error,
                extra={"document_id": str(document.document_id)},
            )
            return None

    async def _remember(self, document: Document, transcript: Transcript) -> None:
        try:
            await self.cache.put(document.document_id, transcript)
        except Exception as error:
            logger.log(
                failure_level(error),
                "transcript_cache_write_failed",
                exc_info=error,
                extra={"document_id": str(document.document_id)},
            )

    def _decoded(self, document: Document, audio: DecodedAudio) -> None:
        context = {
            "document_id": str(document.document_id),
            "content_type": document.document_format,
            "claimed_seconds": document.duration_seconds,
            "decoded_seconds": round(audio.seconds, 3),
            "max_seconds": self.decoder.max_seconds,
        }
        if audio.truncated:
            self.metrics.recording_decode_truncations.inc()
            logger.warning("recording_decode_truncated", extra=context)
            return
        logger.info("recording_decoded", extra=context)

    async def _transcribe(
        self, document: Document, audio: DecodedAudio, report: ProgressReport
    ) -> Transcript:
        started = self.clock()
        try:
            transcript = await self.client.transcribe(audio, report)
        except TranscriptionError as error:
            logger.exception(
                "recording_transcription_failed",
                extra={"document_id": str(document.document_id), "error": type(error).__name__},
            )
            raise UpstreamUnavailableError(TRANSCRIPTION_RETRY_AFTER_SECONDS) from error
        elapsed = self.clock() - started
        real_time_factor = elapsed / audio.seconds
        self.metrics.transcription_real_time_factor.observe(real_time_factor)
        logger.info(
            "recording_transcribed",
            extra={
                "document_id": str(document.document_id),
                "audio_seconds": round(audio.seconds, 3),
                "transcription_seconds": round(elapsed, 3),
                "real_time_factor": round(real_time_factor, 3),
                "language": transcript.language,
                "segments": len(transcript.segments),
            },
        )
        return transcript

    def _material(self, document: Document, transcript: Transcript) -> tuple[SourceMaterial, ...]:
        title = clean_title(document.filename)
        material: list[SourceMaterial] = []
        remaining = self.max_characters
        for passage in passages(transcript.segments, self.passage_ms):
            text = usable_text(passage.text[: remaining * RAW_WINDOW_MULTIPLIER], remaining)
            if text:
                source = TranscriptSource(
                    title=title,
                    document_id=document.document_id,
                    start_ms=passage.start_ms,
                    end_ms=max(passage.end_ms, passage.start_ms + 1),
                )
                material.append(SourceMaterial(source=source, text=text))
                remaining -= len(text)
            if remaining < MIN_VISIBLE_CHARACTERS:
                break
        visible = sum(visible_length(entry.text) for entry in material)
        logger.log(
            logging.INFO if visible >= MIN_VISIBLE_CHARACTERS else logging.WARNING,
            "transcript_parsed",
            extra={
                "document_id": str(document.document_id),
                "passages": len(material),
                "characters": self.max_characters - remaining,
                "budget_exhausted": remaining < MIN_VISIBLE_CHARACTERS,
            },
        )
        if visible < MIN_VISIBLE_CHARACTERS:
            message = f"the recording has {visible} visible characters of transcribed speech"
            raise DocumentHasNoTextError(message)
        return tuple(material)
