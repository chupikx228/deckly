import asyncio
import shutil
from asyncio.subprocess import DEVNULL, PIPE
from pathlib import Path

from deckly.domain.document import DocumentFormat

FFMPEG = "ffmpeg"
EXTRACTION_TIMEOUT_SECONDS = 30
TRANSCRIPTION_SAMPLE_RATE = "16000"
DEMUXERS = {
    DocumentFormat.MP3: "mp3",
    DocumentFormat.WAV: "wav",
    DocumentFormat.M4A: "mov",
    DocumentFormat.MP4: "mov",
    DocumentFormat.MOV: "mov",
}


def ffmpeg_binary() -> str:
    binary = shutil.which(FFMPEG)
    if binary is None:
        message = "the integration suite needs ffmpeg: brew install ffmpeg, or apt-get install ffmpeg"
        raise AssertionError(message)
    return binary


def extraction_arguments(
    source: Path, document_format: DocumentFormat, max_seconds: int, output: Path
) -> list[str]:
    return [
        ffmpeg_binary(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-f",
        DEMUXERS[document_format],
        "-i",
        str(source),
        "-t",
        str(max_seconds),
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-ac",
        "1",
        "-ar",
        TRANSCRIPTION_SAMPLE_RATE,
        "-c:a",
        "pcm_s16le",
        "-threads",
        "1",
        "-fflags",
        "+bitexact",
        "-y",
        str(output),
    ]


async def extract_audio(
    source: Path, document_format: DocumentFormat, max_seconds: int, output: Path
) -> None:
    process = await asyncio.create_subprocess_exec(
        *extraction_arguments(source, document_format, max_seconds, output), stdout=DEVNULL, stderr=PIPE
    )
    try:
        async with asyncio.timeout(EXTRACTION_TIMEOUT_SECONDS):
            _, stderr = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        message = f"ffmpeg exited with {process.returncode}: {stderr.decode(errors='replace')}"
        raise AssertionError(message)
