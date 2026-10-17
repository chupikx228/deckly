import asyncio
from dataclasses import dataclass

from deckly.application.ports import InspectedUpload
from deckly.domain.exceptions import UnsupportedDocumentError
from deckly.infrastructure.recordings.duration import DurationLimit
from deckly.infrastructure.recordings.isobmff import is_isobmff, probe_isobmff
from deckly.infrastructure.recordings.mpeg_audio import is_mpeg_audio, probe_mpeg_audio
from deckly.infrastructure.recordings.wave import is_wave, probe_wave


def detect_recording(content: bytes, max_duration_seconds: int) -> InspectedUpload:
    limit = DurationLimit(max_duration_seconds)
    if is_isobmff(content):
        return probe_isobmff(content, limit)
    if is_wave(content):
        return probe_wave(content, limit)
    if is_mpeg_audio(content):
        return probe_mpeg_audio(content, limit)
    message = "the uploaded file is not an MP3, M4A, WAV, MP4 or MOV recording"
    raise UnsupportedDocumentError(message)


@dataclass(frozen=True, slots=True)
class ContentRecordingInspector:
    max_duration_seconds: int

    async def identify(self, content: bytes) -> InspectedUpload:
        return await asyncio.to_thread(detect_recording, content, self.max_duration_seconds)
