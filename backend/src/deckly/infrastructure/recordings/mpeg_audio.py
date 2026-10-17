from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from deckly.application.ports import InspectedUpload
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import UnsupportedDocumentError
from deckly.infrastructure.recordings.binary import little_endian
from deckly.infrastructure.recordings.duration import DurationLimit

ID3V2_MAGIC = b"ID3"
ID3V2_HEADER_BYTES = 10
ID3V2_FOOTER_BYTES = 10
ID3V2_FOOTER_FLAG = 0x10
ID3V2_VERSIONS = frozenset({2, 3, 4})
SYNCSAFE_BYTE_LIMIT = 0x80
SYNCSAFE_BITS = 7
ID3V1_MAGIC = b"TAG"
ID3V1_BYTES = 128
APE_PREAMBLE = b"APETAGEX"
APE_HEADER_BYTES = 32
APE_SIZE_OFFSET = 12
APE_FLAGS_OFFSET = 20
APE_FIELD_BYTES = 4
APE_IS_HEADER_FLAG = 1 << 29
MAX_TAGS = 4
MAX_LEADING_PADDING_BYTES = 64 * 1024
MAX_TRAILING_TAG_BYTES = 8 * 1024
FRAME_HEADER_BYTES = 4
MIN_FRAMES = 2
SYNC_BYTE = 0xFF
SYNC_MASK = 0xE0
MPEG_1 = 0b11
MPEG_2 = 0b10
MPEG_2_5 = 0b00
LAYER_III = 0b01
FREE_BITRATE = 0
BAD_BITRATE = 15
RESERVED_SAMPLE_RATE = 3
BITS_PER_KILOBIT = 1000
MPEG_1_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
MPEG_2_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


@dataclass(frozen=True, slots=True)
class Version:
    bitrates_kbps: tuple[int, ...]
    sample_rates: tuple[int, int, int]
    samples_per_frame: int
    length_coefficient: int


VERSIONS: Mapping[int, Version] = MappingProxyType(
    {
        MPEG_1: Version(MPEG_1_BITRATES, (44100, 48000, 32000), 1152, 144),
        MPEG_2: Version(MPEG_2_BITRATES, (22050, 24000, 16000), 576, 72),
        MPEG_2_5: Version(MPEG_2_BITRATES, (11025, 12000, 8000), 576, 72),
    }
)


def unsupported(message: str) -> UnsupportedDocumentError:
    return UnsupportedDocumentError(message)


@dataclass(frozen=True, slots=True)
class Frame:
    sample_rate: int
    samples: int
    length: int


def frame_at(content: bytes, position: int) -> Frame | None:
    if len(content) - position < FRAME_HEADER_BYTES:
        return None
    first, second, third = content[position : position + 3]
    if first != SYNC_BYTE or second & SYNC_MASK != SYNC_MASK:
        return None
    version = VERSIONS.get((second >> 3) & 0b11)
    bitrate_index = third >> 4
    rate_index = (third >> 2) & 0b11
    if (
        version is None
        or (second >> 1) & 0b11 != LAYER_III
        or bitrate_index in {FREE_BITRATE, BAD_BITRATE}
        or rate_index == RESERVED_SAMPLE_RATE
    ):
        return None
    sample_rate = version.sample_rates[rate_index]
    bitrate = version.bitrates_kbps[bitrate_index] * BITS_PER_KILOBIT
    padding = (third >> 1) & 1
    return Frame(
        sample_rate=sample_rate,
        samples=version.samples_per_frame,
        length=version.length_coefficient * bitrate // sample_rate + padding,
    )


def syncsafe(raw: bytes) -> int:
    if any(byte >= SYNCSAFE_BYTE_LIMIT for byte in raw):
        message = "the recording's ID3 tag has a malformed size"
        raise unsupported(message)
    size = 0
    for byte in raw:
        size = (size << SYNCSAFE_BITS) | byte
    return size


def after_id3v2(content: bytes, position: int) -> int:
    if len(content) - position < ID3V2_HEADER_BYTES or content[position + 3] not in ID3V2_VERSIONS:
        message = "the recording's ID3 tag header is malformed"
        raise unsupported(message)
    flags = content[position + 5]
    end = position + ID3V2_HEADER_BYTES + syncsafe(content[position + 6 : position + ID3V2_HEADER_BYTES])
    if flags & ID3V2_FOOTER_FLAG:
        end += ID3V2_FOOTER_BYTES
    if end > len(content):
        message = "the recording's ID3 tag runs past the end of the file"
        raise unsupported(message)
    return end


def after_ape_tag(content: bytes, position: int) -> int:
    if len(content) - position < APE_HEADER_BYTES:
        message = "the recording's APE tag is truncated"
        raise unsupported(message)
    size = little_endian(content, position + APE_SIZE_OFFSET, APE_FIELD_BYTES)
    flags = little_endian(content, position + APE_FLAGS_OFFSET, APE_FIELD_BYTES)
    if not flags & APE_IS_HEADER_FLAG:
        message = "the recording has an APE tag footer where a header belongs"
        raise unsupported(message)
    end = position + APE_HEADER_BYTES + size
    if end > len(content):
        message = "the recording's APE tag runs past the end of the file"
        raise unsupported(message)
    return end


def is_zero_padding(content: bytes, position: int) -> bool:
    return content.count(0, position) == len(content) - position


def check_trailing_tags(content: bytes, position: int) -> None:
    tags_start = position
    for _ in range(MAX_TAGS):
        if position - tags_start > MAX_TRAILING_TAG_BYTES:
            message = f"the recording's trailing tags are larger than {MAX_TRAILING_TAG_BYTES} bytes"
            raise unsupported(message)
        remaining = len(content) - position
        if remaining < FRAME_HEADER_BYTES or is_zero_padding(content, position):
            return
        if content.startswith(ID3V1_MAGIC, position) and remaining == ID3V1_BYTES:
            return
        if content.startswith(APE_PREAMBLE, position):
            position = after_ape_tag(content, position)
        elif content.startswith(ID3V2_MAGIC, position):
            position = after_id3v2(content, position)
        else:
            message = "the recording has data after its last audio frame that is not a known tag"
            raise unsupported(message)
    if position != len(content) or position - tags_start > MAX_TRAILING_TAG_BYTES:
        message = "the recording ends in more or larger tags than an audio file carries"
        raise unsupported(message)


def audio_start(content: bytes) -> int:
    position = 0
    for _ in range(MAX_TAGS):
        if not content.startswith(ID3V2_MAGIC, position):
            break
        position = after_id3v2(content, position)
    window = content[position : position + MAX_LEADING_PADDING_BYTES]
    return position + len(window) - len(window.lstrip(b"\x00"))


def is_mpeg_audio(content: bytes) -> bool:
    return content.startswith(ID3V2_MAGIC) or frame_at(content, 0) is not None


def probe_mpeg_audio(content: bytes, limit: DurationLimit) -> InspectedUpload:
    position = audio_start(content)
    first = frame_at(content, position)
    if first is None:
        message = "the recording is not MPEG audio layer III"
        raise unsupported(message)
    ceiling = limit.units(first.sample_rate)
    samples = 0
    frames = 0
    frame: Frame | None = first
    while frame is not None:
        if frame.sample_rate != first.sample_rate:
            message = "the recording changes its MPEG version or sample rate mid-stream"
            raise unsupported(message)
        samples += frame.samples
        frames += 1
        if samples > ceiling:
            raise limit.too_long()
        position += frame.length
        frame = frame_at(content, position)
    if position < len(content):
        check_trailing_tags(content, position)
    if frames < MIN_FRAMES:
        message = "the recording has fewer than two MPEG audio frames"
        raise unsupported(message)
    return InspectedUpload(document_format=DocumentFormat.MP3, duration_seconds=samples / first.sample_rate)
