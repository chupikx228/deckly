from dataclasses import dataclass

from deckly.application.ports import InspectedUpload
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import UnsupportedDocumentError
from deckly.infrastructure.recordings.binary import little_endian
from deckly.infrastructure.recordings.duration import DurationLimit

RIFF = b"RIFF"
WAVE = b"WAVE"
FORMAT_CHUNK = b"fmt "
DATA_CHUNK = b"data"
RIFF_HEADER_BYTES = 12
CHUNK_HEADER_BYTES = 8
CHUNK_ID_BYTES = 4
CHUNK_SIZE_BYTES = 4
FORMAT_BYTES = 16
EXTENSIBLE_FORMAT_BYTES = 40
SUBFORMAT_OFFSET = 24
SUBFORMAT_CODE_BYTES = 2
SUBFORMAT_BYTES = 16
MAX_CHUNKS = 256
PCM = 0x0001
IEEE_FLOAT = 0x0003
EXTENSIBLE = 0xFFFE
SUBFORMAT_GUID_TAIL = bytes.fromhex("000000001000800000aa00389b71")
BITS_BY_ENCODING = {PCM: frozenset({8, 16, 24, 32}), IEEE_FLOAT: frozenset({32, 64})}
MAX_CHANNELS = 8
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 384000
BITS_PER_BYTE = 8


def unsupported(message: str) -> UnsupportedDocumentError:
    return UnsupportedDocumentError(message)


@dataclass(frozen=True, slots=True)
class WaveFormat:
    byte_rate: int
    block_align: int


def encoding_of(content: bytes, start: int, size: int, tag: int) -> int:
    if tag != EXTENSIBLE:
        return tag
    if size < EXTENSIBLE_FORMAT_BYTES:
        message = "the recording's extensible format chunk is too short"
        raise unsupported(message)
    subformat = start + SUBFORMAT_OFFSET
    if content[subformat + SUBFORMAT_CODE_BYTES : subformat + SUBFORMAT_BYTES] != SUBFORMAT_GUID_TAIL:
        message = "the recording's extensible format names an unknown subformat"
        raise unsupported(message)
    return little_endian(content, subformat, SUBFORMAT_CODE_BYTES)


def read_format(content: bytes, start: int, size: int) -> WaveFormat:
    if size < FORMAT_BYTES:
        message = "the recording's format chunk is too short"
        raise unsupported(message)
    tag = little_endian(content, start, 2)
    channels = little_endian(content, start + 2, 2)
    sample_rate = little_endian(content, start + 4, 4)
    byte_rate = little_endian(content, start + 8, 4)
    block_align = little_endian(content, start + 12, 2)
    bits = little_endian(content, start + 14, 2)
    encoding = encoding_of(content, start, size, tag)
    if (
        bits not in BITS_BY_ENCODING.get(encoding, frozenset())
        or not 1 <= channels <= MAX_CHANNELS
        or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
        or block_align != channels * bits // BITS_PER_BYTE
        or byte_rate != sample_rate * block_align
    ):
        message = "the recording is not uncompressed PCM or floating-point audio"
        raise unsupported(message)
    return WaveFormat(byte_rate=byte_rate, block_align=block_align)


def is_wave(content: bytes) -> bool:
    return content.startswith(RIFF) and content[8:RIFF_HEADER_BYTES] == WAVE


def probe_wave(content: bytes, limit: DurationLimit) -> InspectedUpload:
    position = RIFF_HEADER_BYTES
    wave_format: WaveFormat | None = None
    for _ in range(MAX_CHUNKS):
        if len(content) - position < CHUNK_HEADER_BYTES:
            break
        kind = content[position : position + CHUNK_ID_BYTES]
        size = little_endian(content, position + CHUNK_ID_BYTES, CHUNK_SIZE_BYTES)
        start = position + CHUNK_HEADER_BYTES
        if kind == DATA_CHUNK:
            return audio_after(content, start, wave_format, limit)
        if size > len(content) - start:
            message = f"the recording's {kind!r} chunk runs past the end of the file"
            raise unsupported(message)
        if kind == FORMAT_CHUNK:
            if wave_format is not None:
                message = "the recording has more than one format chunk"
                raise unsupported(message)
            wave_format = read_format(content, start, size)
        position = start + size + (size & 1)
    message = "the recording has no audio data chunk"
    raise unsupported(message)


def audio_after(
    content: bytes, start: int, wave_format: WaveFormat | None, limit: DurationLimit
) -> InspectedUpload:
    if wave_format is None:
        message = "the recording's audio data comes before its format"
        raise unsupported(message)
    audio_bytes = len(content) - start
    if audio_bytes < wave_format.block_align:
        message = "the recording has no audio"
        raise unsupported(message)
    limit.check(audio_bytes, wave_format.byte_rate)
    return InspectedUpload(
        document_format=DocumentFormat.WAV, duration_seconds=audio_bytes / wave_format.byte_rate
    )
