from deckly.infrastructure.transcription.decoding import FfmpegAudioDecoder
from deckly.infrastructure.transcription.model import require_ffmpeg

EXTRACTION_TIMEOUT_SECONDS = 30


def production_decoder(max_seconds: int) -> FfmpegAudioDecoder:
    return FfmpegAudioDecoder(
        binary=require_ffmpeg(), max_seconds=max_seconds, deadline_seconds=EXTRACTION_TIMEOUT_SECONDS
    )
