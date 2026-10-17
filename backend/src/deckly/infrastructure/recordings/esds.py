from dataclasses import dataclass

from deckly.infrastructure.recordings.boxes import FULL_BOX_PREFIX_BYTES, Box, Container, field, unsupported
from deckly.infrastructure.recordings.duration import PacketFloor

DECODER_CONFIGURATION = b"esds"
QUICKTIME_WAVE = b"wave"
SAMPLE_ENTRY_RESERVED_BYTES = 8
SOUND_VERSION_BYTES = 2
SOUND_ENTRY_FIXED_BYTES = {0: 28, 1: 44, 2: 64}
ES_DESCRIPTOR = 0x03
DECODER_CONFIG_DESCRIPTOR = 0x04
DECODER_SPECIFIC_INFO = 0x05
STREAM_DEPENDENCE = 0x80
URL_PRESENT = 0x40
OCR_STREAM = 0x20
ES_ID_BYTES = 2
DEPENDENCY_BYTES = 2
OCR_ID_BYTES = 2
DECODER_CONFIG_AFTER_OBJECT_TYPE_BYTES = 12
MAX_SIZE_BYTES = 4
SIZE_CONTINUES = 0x80
SIZE_BITS = 0x7F
SIZE_SHIFT = 7
MPEG4_AUDIO = 0x40
MPEG2_AAC = frozenset({0x66, 0x67, 0x68})
AAC_OBJECT_TYPE_INDICATIONS = MPEG2_AAC | {MPEG4_AUDIO}
MAX_SPECIFIC_INFO_BYTES = 64
OBJECT_TYPE_BITS = 5
ESCAPED_OBJECT_TYPE = 31
ESCAPED_OBJECT_TYPE_BITS = 6
ESCAPED_OBJECT_TYPE_BASE = 32
FREQUENCY_INDEX_BITS = 4
EXPLICIT_FREQUENCY_INDEX = 15
EXPLICIT_FREQUENCY_BITS = 24
SAMPLING_FREQUENCIES = (
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
)
MIN_AAC_SAMPLE_RATE = 7350
MAX_AAC_SAMPLE_RATE = 96000
LOW_DELAY_OBJECT_TYPES = frozenset({23, 39})
SHORTEST_AAC_FRAME = 960
SHORTEST_LOW_DELAY_AAC_FRAME = 480


def cut_short() -> Exception:
    return unsupported("the recording's audio decoder configuration is cut short")


class DescriptorReader:
    def __init__(self, content: bytes, start: int, end: int) -> None:
        self._content = content
        self._position = start
        self._end = end

    def byte(self) -> int:
        if self._position >= self._end:
            raise cut_short()
        value = self._content[self._position]
        self._position += 1
        return value

    def skip(self, count: int) -> None:
        if self._position + count > self._end:
            raise cut_short()
        self._position += count

    def descriptor(self, tag: int) -> "DescriptorReader":
        if self.byte() != tag:
            message = f"the recording's audio decoder configuration has no descriptor {tag:#x} where expected"
            raise unsupported(message)
        size = 0
        for _ in range(MAX_SIZE_BYTES):
            part = self.byte()
            size = (size << SIZE_SHIFT) | (part & SIZE_BITS)
            if not part & SIZE_CONTINUES:
                break
        else:
            message = "the recording's audio decoder configuration has a descriptor size over four bytes"
            raise unsupported(message)
        start = self._position
        self.skip(size)
        return DescriptorReader(self._content, start, start + size)

    def remaining(self, limit: int) -> bytes:
        return self._content[self._position : min(self._end, self._position + limit)]


class BitReader:
    def __init__(self, data: bytes) -> None:
        self._value = int.from_bytes(data, "big")
        self._left = len(data) * 8

    def read(self, count: int) -> int:
        if count > self._left:
            raise cut_short()
        self._left -= count
        return (self._value >> self._left) & ((1 << count) - 1)


@dataclass(frozen=True, slots=True)
class AacConfiguration:
    object_type: int
    sample_rate: int

    @property
    def floor(self) -> PacketFloor:
        frames = (
            SHORTEST_LOW_DELAY_AAC_FRAME if self.object_type in LOW_DELAY_OBJECT_TYPES else SHORTEST_AAC_FRAME
        )
        return PacketFloor(frames_per_packet=frames, sample_rate=self.sample_rate)


def audio_specific_config(data: bytes) -> AacConfiguration:
    bits = BitReader(data)
    object_type = bits.read(OBJECT_TYPE_BITS)
    if object_type == ESCAPED_OBJECT_TYPE:
        object_type = ESCAPED_OBJECT_TYPE_BASE + bits.read(ESCAPED_OBJECT_TYPE_BITS)
    index = bits.read(FREQUENCY_INDEX_BITS)
    if index == EXPLICIT_FREQUENCY_INDEX:
        rate = bits.read(EXPLICIT_FREQUENCY_BITS)
    elif index < len(SAMPLING_FREQUENCIES):
        rate = SAMPLING_FREQUENCIES[index]
    else:
        message = "the recording's AAC audio uses a reserved sampling frequency index"
        raise unsupported(message)
    if not MIN_AAC_SAMPLE_RATE <= rate <= MAX_AAC_SAMPLE_RATE:
        message = "the recording's AAC audio declares an implausible sample rate"
        raise unsupported(message)
    return AacConfiguration(object_type=object_type, sample_rate=rate)


def aac_configuration(content: bytes, configuration: Box) -> AacConfiguration | None:
    reader = DescriptorReader(content, configuration.start + FULL_BOX_PREFIX_BYTES, configuration.end)
    stream = reader.descriptor(ES_DESCRIPTOR)
    stream.skip(ES_ID_BYTES)
    flags = stream.byte()
    if flags & STREAM_DEPENDENCE:
        stream.skip(DEPENDENCY_BYTES)
    if flags & URL_PRESENT:
        stream.skip(stream.byte())
    if flags & OCR_STREAM:
        stream.skip(OCR_ID_BYTES)
    decoder = stream.descriptor(DECODER_CONFIG_DESCRIPTOR)
    object_type_indication = decoder.byte()
    if object_type_indication not in AAC_OBJECT_TYPE_INDICATIONS:
        return None
    decoder.skip(DECODER_CONFIG_AFTER_OBJECT_TYPE_BYTES)
    specific = decoder.descriptor(DECODER_SPECIFIC_INFO)
    return audio_specific_config(specific.remaining(MAX_SPECIFIC_INFO_BYTES))


def first_sample_entry(container: Container, description: Box) -> Box:
    return container.box_at(description.start + SAMPLE_ENTRY_RESERVED_BYTES, description.end)


def decoder_configuration(container: Container, entry: Box) -> Box | None:
    version = field(container.content, entry, SAMPLE_ENTRY_RESERVED_BYTES, SOUND_VERSION_BYTES)
    fixed = SOUND_ENTRY_FIXED_BYTES.get(version)
    if fixed is None or entry.size < fixed:
        message = f"the recording's sound description version {version} is not supported"
        raise unsupported(message)
    found: list[Box] = []
    for child in container.children(entry.start + fixed, entry.end):
        if child.kind == DECODER_CONFIGURATION:
            found.append(child)
        elif child.kind == QUICKTIME_WAVE:
            found.extend(
                inner
                for inner in container.children(child.start, child.end)
                if inner.kind == DECODER_CONFIGURATION
            )
    if len(found) > 1:
        message = "the recording has more than one audio decoder configuration"
        raise unsupported(message)
    return found[0] if found else None
