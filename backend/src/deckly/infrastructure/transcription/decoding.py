import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from deckly.application.exceptions import DocumentHasNoTextError, UnreadableDocumentError
from deckly.domain.document import DocumentFormat
from deckly.infrastructure.transcription.client import (
    BYTES_PER_SECOND,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    DecodedAudio,
)
from deckly.infrastructure.transcription.process import Stop, supervise

READ_CHUNK_BYTES = 64 * 1024
OVERFLOW_PROBE_BYTES = 1
DEMUXERS: Mapping[DocumentFormat, str] = MappingProxyType(
    {
        DocumentFormat.MP3: "mp3",
        DocumentFormat.WAV: "wav",
        DocumentFormat.M4A: "mov",
        DocumentFormat.MP4: "mov",
        DocumentFormat.MOV: "mov",
    }
)
DECODER_ENVIRONMENT: Mapping[str, str] = MappingProxyType({"PATH": "", "LC_ALL": "C.UTF-8"})


@dataclass(frozen=True, slots=True)
class Copied:
    written: int
    truncated: bool


async def copy_capped(source: asyncio.StreamReader, target: Path, max_bytes: int, stop: Stop) -> Copied:
    written = 0
    with target.open("wb") as file:
        while written < max_bytes:
            chunk = await source.read(min(READ_CHUNK_BYTES, max_bytes - written))
            if not chunk:
                return Copied(written=written, truncated=False)
            file.write(chunk)
            written += len(chunk)
    if not await source.read(OVERFLOW_PROBE_BYTES):
        return Copied(written=written, truncated=False)
    stop()
    return Copied(written=written, truncated=True)


@dataclass(frozen=True, slots=True)
class FfmpegAudioDecoder:
    binary: Path
    max_seconds: int
    deadline_seconds: float

    @property
    def max_bytes(self) -> int:
        return self.max_seconds * BYTES_PER_SECOND

    def arguments(self, source: Path, document_format: DocumentFormat) -> list[str]:
        demuxer = DEMUXERS.get(document_format)
        if demuxer is None:
            message = f"recordings of type {document_format} have no audio decoder"
            raise UnreadableDocumentError(message)
        return [
            str(self.binary),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-f",
            demuxer,
            "-i",
            str(source),
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-threads",
            "1",
            "pipe:1",
        ]

    async def decode(self, source: Path, document_format: DocumentFormat, target: Path) -> DecodedAudio:
        source = await asyncio.to_thread(source.resolve)
        arguments = self.arguments(source, document_format)

        async def consume(stdout: asyncio.StreamReader, stop: Stop) -> Copied:
            return await copy_capped(stdout, target, self.max_bytes, stop)

        try:
            async with asyncio.timeout(self.deadline_seconds):
                finished = await supervise(
                    arguments, environment=DECODER_ENVIRONMENT, workdir=source.parent, consume=consume
                )
        except TimeoutError as error:
            message = f"decoding the recording took longer than {self.deadline_seconds} seconds"
            raise UnreadableDocumentError(message) from error
        copied = finished.output
        if finished.returncode != 0 and not copied.truncated:
            message = f"ffmpeg could not decode the recording: {finished.last_error_line}"
            raise UnreadableDocumentError(message)
        samples = copied.written // SAMPLE_WIDTH_BYTES
        if samples == 0:
            message = "the recording has no audio to transcribe"
            raise DocumentHasNoTextError(message)
        return DecodedAudio(path=target, samples=samples, truncated=copied.truncated)
