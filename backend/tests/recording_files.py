import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "recordings"
MPEG_1 = 0b11
MPEG_2 = 0b10
MPEG_2_5 = 0b00
LAYER_III = 0b01
LAYER_II = 0b10
NO_CRC = 1
AAC_FRAME_SAMPLES = 1024
MOVIE_TIMESCALE = 1000
SOUND_TIMESCALE = 16000
UNKNOWN_DURATION_32 = 0xFFFF_FFFF
PCM = 1
IEEE_FLOAT = 3
EXTENSIBLE = 0xFFFE
SUBFORMAT_GUID_TAIL = bytes.fromhex("000000001000800000aa00389b71")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def box(kind: bytes, *payloads: bytes) -> bytes:
    payload = b"".join(payloads)
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def large_box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 1) + kind + struct.pack(">Q", 16 + len(payload)) + payload


def full_box(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]), payload)


def file_type(major: bytes = b"isom", compatible: Sequence[bytes] = (b"isom", b"mp41")) -> bytes:
    return box(b"ftyp", major, b"\x00\x00\x02\x00", *compatible)


def timing(timescale: int, duration: int, version: int) -> bytes:
    if version == 1:
        return struct.pack(">QQIQ", 0, 0, timescale, duration)
    return struct.pack(">IIII", 0, 0, timescale, duration)


def movie_header(duration: int, timescale: int = MOVIE_TIMESCALE, version: int = 0) -> bytes:
    return full_box(b"mvhd", timing(timescale, duration, version) + b"\x00" * 80, version)


def media_header(duration: int, timescale: int = SOUND_TIMESCALE, version: int = 0) -> bytes:
    return full_box(b"mdhd", timing(timescale, duration, version) + b"\x00" * 4, version)


def handler(kind: bytes) -> bytes:
    return full_box(b"hdlr", b"\x00" * 4 + kind + b"\x00" * 13)


SOUND_ENTRY_EXTRA_BYTES = {0: 0, 1: 16, 2: 36}
MPEG4_AUDIO = 0x40
MPEG1_AUDIO = 0x6B
AAC_LOW_COMPLEXITY = 2
FREQUENCY_INDEX_16000 = 8
EXPLICIT_FREQUENCY_INDEX = 15


def descriptor(tag: int, body: bytes) -> bytes:
    return bytes([tag, len(body)]) + body


def audio_specific_config(
    frequency_index: int = FREQUENCY_INDEX_16000,
    object_type: int = AAC_LOW_COMPLEXITY,
    explicit_rate: int | None = None,
) -> bytes:
    value, width = (object_type, 5) if object_type < 31 else ((31 << 6) | (object_type - 32), 11)
    value, width = (value << 4) | frequency_index, width + 4
    if frequency_index == EXPLICIT_FREQUENCY_INDEX:
        value, width = (value << 24) | (explicit_rate or 0), width + 24
    value, width = (value << 4) | 1, width + 4
    padding = -width % 8
    return (value << padding).to_bytes((width + padding) // 8, "big")


def decoder_configuration(
    specific: bytes | None = None, object_type_indication: int = MPEG4_AUDIO, *, raw: bytes | None = None
) -> bytes:
    if raw is not None:
        return full_box(b"esds", raw)
    info = descriptor(5, audio_specific_config() if specific is None else specific)
    decoder = descriptor(4, bytes([object_type_indication, 0x15]) + b"\x00" * 11 + info)
    return full_box(b"esds", descriptor(3, b"\x00\x01\x00" + decoder))


def sound_entry(version: int = 0, children: bytes | None = None) -> bytes:
    fixed = b"\x00" * 6 + struct.pack(">HHH4xHHHHI", 1, version, 0, 1, 16, 0, 0, SOUND_TIMESCALE << 16)
    extra = b"\x00" * SOUND_ENTRY_EXTRA_BYTES.get(version, 0)
    return fixed + extra + (decoder_configuration() if children is None else children)


def sample_description(entry_format: bytes = b"mp4a", entry: bytes | None = None) -> bytes:
    default = sound_entry() if entry_format == b"mp4a" else sound_entry(children=b"")
    return full_box(b"stsd", struct.pack(">I", 1) + box(entry_format, default if entry is None else entry))


def time_to_sample(entries: Sequence[tuple[int, int]], declared: int | None = None) -> bytes:
    count = len(entries) if declared is None else declared
    return full_box(
        b"stts", struct.pack(">I", count) + b"".join(struct.pack(">II", n, delta) for n, delta in entries)
    )


def uniform_time_to_sample(count: int, delta: int) -> bytes:
    return full_box(b"stts", struct.pack(">I", count) + struct.pack(">II", 1, delta) * count)


def sampled_count(stts: bytes) -> int:
    entries = (len(stts) - 16) // 8
    return sum(struct.unpack_from(">I", stts, 16 + 8 * index)[0] for index in range(entries))


def sample_sizes(count: int, kind: bytes = b"stsz") -> bytes:
    if kind == b"stz2":
        return full_box(b"stz2", b"\x00\x00\x00\x08" + struct.pack(">I", count) + b"\x06" * count)
    return full_box(b"stsz", struct.pack(">II", 6, count))


def frames_for(seconds: float, timescale: int = SOUND_TIMESCALE) -> list[tuple[int, int]]:
    return [(round(seconds * timescale / AAC_FRAME_SAMPLES), AAC_FRAME_SAMPLES)]


@dataclass(frozen=True, slots=True)
class Track:
    kind: bytes = b"soun"
    seconds: float = 2
    media_duration: int | None = None
    stts: bytes | None = None
    entry_format: bytes = b"mp4a"
    sizes: bytes | None = None
    extra_media: bytes = b""
    extra_samples: bytes = b""
    header: bytes = b""
    entry: bytes | None = None

    def encode(self) -> bytes:
        duration = (
            round(self.seconds * SOUND_TIMESCALE) if self.media_duration is None else self.media_duration
        )
        table = time_to_sample(frames_for(self.seconds)) if self.stts is None else self.stts
        sizes = sample_sizes(sampled_count(table)) if self.sizes is None else self.sizes
        samples = box(
            b"stbl", sample_description(self.entry_format, self.entry), table, sizes, self.extra_samples
        )
        return box(
            b"trak",
            self.header,
            box(
                b"mdia",
                media_header(duration),
                handler(self.kind),
                box(b"minf", samples),
                self.extra_media,
            ),
        )


def track(
    kind: bytes = b"soun", *, seconds: float = 2, media_duration: int | None = None, stts: bytes | None = None
) -> bytes:
    return Track(kind=kind, seconds=seconds, media_duration=media_duration, stts=stts).encode()


def movie(*children: bytes, seconds: float = 2, header: bytes | None = None) -> bytes:
    return box(
        b"moov", movie_header(round(seconds * MOVIE_TIMESCALE)) if header is None else header, *children
    )


def isobmff(
    *top_level: bytes, brand: bytes = b"isom", compatible: Sequence[bytes] = (b"isom", b"mp41")
) -> bytes:
    return file_type(brand, compatible) + b"".join(top_level)


def audio_mp4(seconds: float = 2, *, brand: bytes = b"M4A ", mdat: bytes = b"\x00" * 64) -> bytes:
    return isobmff(movie(track(seconds=seconds), seconds=seconds), box(b"mdat", mdat), brand=brand)


DEFAULT_BASE_IS_MOOF = 0x020000
BASE_DATA_OFFSET_PRESENT = 0x000001
SAMPLE_DESCRIPTION_INDEX_PRESENT = 0x000002
DEFAULT_SAMPLE_DURATION_PRESENT = 0x000008
DATA_OFFSET_PRESENT = 0x000001
FIRST_SAMPLE_FLAGS_PRESENT = 0x000004
SAMPLE_DURATION_PRESENT = 0x000100
SAMPLE_SIZE_PRESENT = 0x000200
SAMPLE_FLAGS_PRESENT = 0x000400
COMPOSITION_OFFSET_PRESENT = 0x000800
AUDIO_TRACK_ID = 1
VIDEO_TRACK_ID = 2


def flagged_box(kind: bytes, flags: int, payload: bytes, version: int = 0) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big"), payload)


def track_header(track_id: int = AUDIO_TRACK_ID, version: int = 0) -> bytes:
    if version == 1:
        return full_box(b"tkhd", struct.pack(">QQII", 0, 0, track_id, 0) + b"\x00" * 60, version)
    return full_box(b"tkhd", struct.pack(">IIII", 0, 0, track_id, 0) + b"\x00" * 64, version)


def track_extends(track_id: int = AUDIO_TRACK_ID, default_duration: int = 0) -> bytes:
    return full_box(b"trex", struct.pack(">IIIII", track_id, 1, default_duration, 0, 0))


def movie_extends(*children: bytes) -> bytes:
    return box(b"mvex", *(children or (track_extends(),)))


def movie_extends_header(duration: int, version: int = 0) -> bytes:
    packed = struct.pack(">Q", duration) if version == 1 else struct.pack(">I", duration)
    return full_box(b"mehd", packed, version)


def fragment_header(
    track_id: int = AUDIO_TRACK_ID,
    default_duration: int | None = AAC_FRAME_SAMPLES,
    *,
    base_data_offset: bool = False,
    description_index: bool = False,
) -> bytes:
    flags = DEFAULT_BASE_IS_MOOF
    payload = struct.pack(">I", track_id)
    if base_data_offset:
        flags |= BASE_DATA_OFFSET_PRESENT
        payload += struct.pack(">Q", 0)
    if description_index:
        flags |= SAMPLE_DESCRIPTION_INDEX_PRESENT
        payload += struct.pack(">I", 1)
    if default_duration is not None:
        flags |= DEFAULT_SAMPLE_DURATION_PRESENT
        payload += struct.pack(">I", default_duration)
    return flagged_box(b"tfhd", flags, payload)


@dataclass(frozen=True, slots=True)
class TrackRun:
    count: int
    durations: Sequence[int] | None = None
    sizes: bool = True
    sample_flags: bool = False
    composition_offsets: bool = False
    first_sample_flags: bool = False
    declared: int | None = None

    def encode(self) -> bytes:
        flags = DATA_OFFSET_PRESENT | (FIRST_SAMPLE_FLAGS_PRESENT if self.first_sample_flags else 0)
        fields = [
            (self.durations is not None, SAMPLE_DURATION_PRESENT),
            (self.sizes, SAMPLE_SIZE_PRESENT),
            (self.sample_flags, SAMPLE_FLAGS_PRESENT),
            (self.composition_offsets, COMPOSITION_OFFSET_PRESENT),
        ]
        for present, flag in fields:
            flags |= flag if present else 0
        header = struct.pack(">Ii", self.count if self.declared is None else self.declared, 0)
        if self.first_sample_flags:
            header += struct.pack(">I", 0)
        records = b"".join(self._record(index) for index in range(self.count))
        return flagged_box(b"trun", flags, header + records)

    def _record(self, index: int) -> bytes:
        record = b""
        if self.durations is not None:
            record += struct.pack(">I", self.durations[index])
        if self.sizes:
            record += struct.pack(">I", 6)
        if self.sample_flags:
            record += struct.pack(">I", 0)
        if self.composition_offsets:
            record += struct.pack(">i", 0)
        return record


def track_run(count: int, durations: Sequence[int] | None = None) -> bytes:
    return TrackRun(count=count, durations=durations).encode()


def track_fragment(header: bytes, *runs: bytes) -> bytes:
    return box(b"traf", header, *runs)


def movie_fragment(*track_fragments: bytes, sequence: int = 1) -> bytes:
    return box(b"moof", full_box(b"mfhd", struct.pack(">I", sequence)), *track_fragments)


def audio_fragment(seconds: float, sequence: int = 1) -> bytes:
    samples = round(seconds * SOUND_TIMESCALE / AAC_FRAME_SAMPLES)
    return movie_fragment(track_fragment(fragment_header(), track_run(samples)), sequence=sequence) + box(
        b"mdat", b"\x00" * 6 * samples
    )


def fragmented_track(track_id: int = AUDIO_TRACK_ID, kind: bytes = b"soun", *, seconds: float = 0) -> bytes:
    return Track(
        kind=kind,
        seconds=seconds,
        stts=time_to_sample(frames_for(seconds)) if seconds else time_to_sample([]),
        header=track_header(track_id),
    ).encode()


def fragmented_movie(
    *fragments: bytes,
    header_seconds: float = 0,
    extends: bytes | None = None,
    tracks: Sequence[bytes] | None = None,
) -> bytes:
    moov = movie(
        *(tracks or (fragmented_track(),)),
        movie_extends() if extends is None else extends,
        seconds=header_seconds,
    )
    return isobmff(moov, *fragments, brand=b"iso5", compatible=(b"iso5", b"iso6", b"mp41", b"dash"))


@dataclass(frozen=True, slots=True)
class MpegFrame:
    length: int
    version: int = MPEG_1
    layer: int = LAYER_III
    bitrate_index: int = 9
    rate_index: int = 0
    padding: int = 0
    body: bytes = b""

    def encode(self) -> bytes:
        header = bytes(
            [
                0xFF,
                0xE0 | (self.version << 3) | (self.layer << 1) | NO_CRC,
                (self.bitrate_index << 4) | (self.rate_index << 2) | (self.padding << 1),
                0xC4,
            ]
        )
        return header + self.body + b"\x00" * (self.length - len(header) - len(self.body))


def cd_frame(body: bytes = b"") -> bytes:
    return MpegFrame(length=144 * 128_000 // 44_100, body=body).encode()


def slow_frame() -> bytes:
    return MpegFrame(version=MPEG_2_5, bitrate_index=1, rate_index=2, length=72).encode()


def shortest_frame() -> bytes:
    return MpegFrame(bitrate_index=1, rate_index=1, length=144 * 32_000 // 48_000).encode()


CD_FRAME_SECONDS = 1152 / 44_100
SLOW_FRAME_SECONDS = 576 / 8_000


def syncsafe(size: int) -> bytes:
    return bytes([(size >> shift) & 0x7F for shift in (21, 14, 7, 0)])


def id3v2(payload: bytes = b"TIT2\x00\x00\x00\x05\x00\x00\x03tone", *, footer: bool = False) -> bytes:
    flags = 0x10 if footer else 0
    tag = b"ID3" + bytes([4, 0, flags]) + syncsafe(len(payload)) + payload
    return tag + (b"3DI" + bytes([4, 0, flags]) + syncsafe(len(payload)) if footer else b"")


def id3v1() -> bytes:
    return b"TAG" + b"\x00" * 125


def ape_tag(items: bytes = b"\x00" * 16, *, header: bool = True) -> bytes:
    size = len(items) + 32
    footer = b"APETAGEX" + struct.pack("<IIII", 2000, size, 1, 0) + b"\x00" * 8
    if not header:
        return items + footer
    return b"APETAGEX" + struct.pack("<IIII", 2000, size, 1, 1 << 29) + b"\x00" * 8 + items + footer


def mp3(frames: int = 80, *, tag: bytes | None = None, frame: bytes | None = None) -> bytes:
    return (id3v2() if tag is None else tag) + (cd_frame() if frame is None else frame) * frames


@dataclass(frozen=True, slots=True)
class WaveFormat:
    tag: int = PCM
    channels: int = 1
    sample_rate: int = 8000
    bits: int = 16
    block_align: int | None = None
    byte_rate: int | None = None

    def encode(self) -> bytes:
        align = self.channels * self.bits // 8 if self.block_align is None else self.block_align
        rate = self.sample_rate * align if self.byte_rate is None else self.byte_rate
        return struct.pack("<HHIIHH", self.tag, self.channels, self.sample_rate, rate, align, self.bits)


def extensible_format(subformat: int = PCM, guid_tail: bytes = SUBFORMAT_GUID_TAIL, bits: int = 16) -> bytes:
    return (
        WaveFormat(tag=EXTENSIBLE, bits=bits).encode()
        + struct.pack("<HHI", 22, bits, 4)
        + struct.pack("<H", subformat)
        + guid_tail
    )


def chunk(kind: bytes, payload: bytes, declared: int | None = None) -> bytes:
    size = len(payload) if declared is None else declared
    return kind + struct.pack("<I", size) + payload + (b"\x00" if len(payload) % 2 else b"")


def wave(*chunks: bytes) -> bytes:
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def pcm_wave(seconds: float = 1, *, sample_rate: int = 8000, declared: int | None = None) -> bytes:
    audio = b"\x00\x01" * round(seconds * sample_rate)
    return wave(chunk(b"fmt ", WaveFormat(sample_rate=sample_rate).encode()), chunk(b"data", audio, declared))


SAMPLE_TABLE_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"})
HEADER_VERSION_0 = 0


@dataclass(frozen=True, slots=True)
class FoundBox:
    kind: bytes
    offset: int
    body: int
    end: int


def walk_boxes(data: bytes, start: int, end: int) -> Iterator[FoundBox]:
    offset = start
    while offset + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, offset + 8)
            header = 16
        found = FoundBox(kind=kind, offset=offset, body=offset + header, end=offset + size)
        yield found
        if kind in SAMPLE_TABLE_CONTAINERS:
            yield from walk_boxes(data, found.body, found.end)
        offset += size


def header_version_0(data: bytearray, found: FoundBox) -> int:
    if data[found.body] != HEADER_VERSION_0:
        message = f"{found.kind!r} is not a version 0 box, which this helper does not rewrite"
        raise AssertionError(message)
    return found.body


def understated_timing(mp4: bytes, delta: int) -> bytes:
    data = bytearray(mp4)
    boxes = list(walk_boxes(mp4, 0, len(mp4)))
    samples = 0
    for found in (box for box in boxes if box.kind == b"stts"):
        (entries,) = struct.unpack_from(">I", data, found.body + 4)
        for index in range(entries):
            entry = found.body + 8 + index * 8
            (count,) = struct.unpack_from(">I", data, entry)
            samples += count * delta
            struct.pack_into(">I", data, entry + 4, delta)
    [media] = (box for box in boxes if box.kind == b"mdhd")
    (media_timescale,) = struct.unpack_from(">I", data, header_version_0(data, media) + 12)
    struct.pack_into(">I", data, media.body + 16, samples)
    [movie] = (box for box in boxes if box.kind == b"mvhd")
    (movie_timescale,) = struct.unpack_from(">I", data, header_version_0(data, movie) + 12)
    movie_duration = samples * movie_timescale // media_timescale + 1
    struct.pack_into(">I", data, movie.body + 16, movie_duration)
    for found in boxes:
        if found.kind == b"tkhd":
            struct.pack_into(">I", data, header_version_0(data, found) + 20, movie_duration)
        if found.kind == b"edts":
            data[found.offset + 4 : found.offset + 8] = b"free"
    return bytes(data)
