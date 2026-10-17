import struct
from collections.abc import Sequence
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


def sample_description(entry_format: bytes = b"mp4a") -> bytes:
    return full_box(b"stsd", struct.pack(">I", 1) + box(entry_format, b"\x00" * 28))


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

    def encode(self) -> bytes:
        duration = (
            round(self.seconds * SOUND_TIMESCALE) if self.media_duration is None else self.media_duration
        )
        table = time_to_sample(frames_for(self.seconds)) if self.stts is None else self.stts
        sizes = sample_sizes(sampled_count(table)) if self.sizes is None else self.sizes
        samples = box(b"stbl", sample_description(self.entry_format), table, sizes, self.extra_samples)
        return box(
            b"trak",
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
