import struct
from collections.abc import Iterator
from dataclasses import dataclass

from deckly.application.ports import InspectedUpload
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import UnsupportedDocumentError
from deckly.infrastructure.recordings.binary import big_endian
from deckly.infrastructure.recordings.duration import DurationLimit

SIZE_BYTES = 4
LARGE_SIZE_BYTES = 8
BOX_HEADER_BYTES = 8
LARGE_BOX_HEADER_BYTES = 16
BITS_PER_BYTE = 8
SIZE_TO_END = 0
LARGE_SIZE = 1
MAX_BOXES = 4096
MAX_TRACKS = 64
MAX_TIME_TO_SAMPLE_ENTRIES = 1_048_576
TIME_TO_SAMPLE_ENTRY_BYTES = 8
FULL_BOX_PREFIX_BYTES = 4
BRAND_BYTES = 4
FILE_TYPE_PREFIX_BYTES = 8
VERSION_1 = 1
FILE_TYPE = b"ftyp"
MOVIE = b"moov"
MOVIE_HEADER = b"mvhd"
MOVIE_EXTENDS = b"mvex"
MOVIE_FRAGMENT = b"moof"
TRACK = b"trak"
MEDIA = b"mdia"
MEDIA_HEADER = b"mdhd"
HANDLER = b"hdlr"
MEDIA_INFORMATION = b"minf"
SAMPLE_TABLE = b"stbl"
SAMPLE_DESCRIPTION = b"stsd"
TIME_TO_SAMPLE = b"stts"
SAMPLE_SIZE = b"stsz"
COMPACT_SAMPLE_SIZE = b"stz2"
SAMPLE_COUNT_OFFSET = 8
SOUND_HANDLER = b"soun"
VIDEO_HANDLER = b"vide"
QUICKTIME_BRAND = b"qt  "
AUDIO_BRANDS = frozenset({b"M4A ", b"M4B "})
MP4_BRANDS = frozenset(
    {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"MSNV"}
)
ACCEPTED_BRANDS = AUDIO_BRANDS | MP4_BRANDS | {QUICKTIME_BRAND}
REFUSED_BRANDS = frozenset({b"M4P ", b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis"})
ENCRYPTED_SAMPLE_ENTRIES = frozenset({b"enca", b"drms"})
HANDLER_TYPE_OFFSET = 8
SAMPLE_ENTRY_FORMAT_OFFSET = 12


def unsupported(message: str) -> UnsupportedDocumentError:
    return UnsupportedDocumentError(message)


@dataclass(frozen=True, slots=True)
class Box:
    kind: bytes
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


class Budget:
    def __init__(self, allowance: int, subject: str) -> None:
        self._remaining = allowance
        self._subject = subject

    def spend(self, amount: int = 1) -> None:
        self._remaining -= amount
        if self._remaining < 0:
            message = f"the recording has too many {self._subject}"
            raise unsupported(message)


@dataclass(frozen=True, slots=True)
class Container:
    content: bytes
    boxes: Budget
    entries: Budget

    def children(self, start: int, end: int) -> Iterator[Box]:
        position = start
        while position < end:
            self.boxes.spend()
            box = self.box_at(position, end)
            yield box
            position = box.end

    def box_at(self, position: int, end: int) -> Box:
        if end - position < BOX_HEADER_BYTES:
            message = "the recording ends inside a box header"
            raise unsupported(message)
        size = big_endian(self.content, position, SIZE_BYTES)
        kind = self.content[position + SIZE_BYTES : position + BOX_HEADER_BYTES]
        header = BOX_HEADER_BYTES
        if size == LARGE_SIZE:
            if end - position < LARGE_BOX_HEADER_BYTES:
                message = "the recording ends inside a box header"
                raise unsupported(message)
            size = big_endian(self.content, position + BOX_HEADER_BYTES, LARGE_SIZE_BYTES)
            header = LARGE_BOX_HEADER_BYTES
        elif size == SIZE_TO_END:
            size = end - position
        if size < header or size > end - position:
            message = f"a {kind!r} box runs past the end of what contains it"
            raise unsupported(message)
        return Box(kind=kind, start=position + header, end=position + size)

    def single(self, parent: Box, kind: bytes) -> Box | None:
        found = [box for box in self.children(parent.start, parent.end) if box.kind == kind]
        if len(found) > 1:
            message = f"the recording has more than one {kind!r} box inside {parent.kind!r}"
            raise unsupported(message)
        return found[0] if found else None

    def required(self, parent: Box, kind: bytes) -> Box:
        box = self.single(parent, kind)
        if box is None:
            message = f"the recording has no {kind!r} box inside {parent.kind!r}"
            raise unsupported(message)
        return box


@dataclass(frozen=True, slots=True)
class Timing:
    timescale: int
    duration: int | None


@dataclass(frozen=True, slots=True)
class SoundTrack:
    timing: Timing
    sampled_duration: int


def read_major_brand(content: bytes, file_type: Box) -> bytes:
    if file_type.size < FILE_TYPE_PREFIX_BYTES:
        message = "the recording's file type box is too short"
        raise unsupported(message)
    major = content[file_type.start : file_type.start + BRAND_BYTES]
    compatible = frozenset(
        content[offset : offset + BRAND_BYTES]
        for offset in range(
            file_type.start + FILE_TYPE_PREFIX_BYTES, file_type.end - BRAND_BYTES + 1, BRAND_BYTES
        )
    )
    if major in REFUSED_BRANDS or not ({major} | compatible) & ACCEPTED_BRANDS:
        message = f"the {major!r} brand is not an audio or video recording this service accepts"
        raise unsupported(message)
    return major


@dataclass(frozen=True, slots=True)
class HeaderLayout:
    timescale_offset: int
    duration_offset: int
    duration_bytes: int

    @property
    def size(self) -> int:
        return self.duration_offset + self.duration_bytes

    @property
    def unknown_duration(self) -> int:
        return (1 << (self.duration_bytes * BITS_PER_BYTE)) - 1


HEADER_V0 = HeaderLayout(timescale_offset=12, duration_offset=16, duration_bytes=4)
HEADER_V1 = HeaderLayout(timescale_offset=20, duration_offset=24, duration_bytes=8)


def read_timing(content: bytes, header: Box) -> Timing:
    layout = HEADER_V1 if header.size >= 1 and content[header.start] == VERSION_1 else HEADER_V0
    if header.size < layout.size:
        message = f"the recording's {header.kind!r} box is too short"
        raise unsupported(message)
    timescale = big_endian(content, header.start + layout.timescale_offset, SIZE_BYTES)
    duration = big_endian(content, header.start + layout.duration_offset, layout.duration_bytes)
    if timescale == 0:
        message = f"the recording's {header.kind!r} box has no timescale"
        raise unsupported(message)
    return Timing(timescale=timescale, duration=None if duration == layout.unknown_duration else duration)


def check_timing(timing: Timing, limit: DurationLimit) -> None:
    if timing.duration is not None:
        limit.check(timing.duration, timing.timescale)


def four_cc_at(content: bytes, box: Box, offset: int) -> bytes:
    if box.size < offset + BRAND_BYTES:
        message = f"the recording's {box.kind!r} box is too short"
        raise unsupported(message)
    return content[box.start + offset : box.start + offset + BRAND_BYTES]


@dataclass(frozen=True, slots=True)
class Sampling:
    duration: int
    samples: int


def sampled_duration(container: Container, table: Box, timing: Timing, limit: DurationLimit) -> Sampling:
    if table.size < FULL_BOX_PREFIX_BYTES + SIZE_BYTES:
        message = "the recording's time-to-sample box is too short"
        raise unsupported(message)
    entry_count = big_endian(container.content, table.start + FULL_BOX_PREFIX_BYTES, SIZE_BYTES)
    entries_start = table.start + FULL_BOX_PREFIX_BYTES + SIZE_BYTES
    if entry_count * TIME_TO_SAMPLE_ENTRY_BYTES > table.end - entries_start:
        message = "the recording's time-to-sample box declares more entries than it holds"
        raise unsupported(message)
    container.entries.spend(entry_count)
    ceiling = limit.units(timing.timescale)
    total = 0
    samples = 0
    entries = memoryview(container.content)[
        entries_start : entries_start + entry_count * TIME_TO_SAMPLE_ENTRY_BYTES
    ]
    for sample_count, sample_delta in struct.iter_unpack(">II", entries):
        samples += int(sample_count)
        total += int(sample_count) * int(sample_delta)
        if total > ceiling:
            raise limit.too_long()
    return Sampling(duration=total, samples=samples)


def stored_samples(container: Container, samples: Box) -> int:
    tables = [
        table
        for table in (container.single(samples, SAMPLE_SIZE), container.single(samples, COMPACT_SAMPLE_SIZE))
        if table is not None
    ]
    if len(tables) != 1:
        message = "the recording must have exactly one sample size table per track"
        raise unsupported(message)
    (table,) = tables
    if table.size < SAMPLE_COUNT_OFFSET + SIZE_BYTES:
        message = f"the recording's {table.kind!r} box is too short"
        raise unsupported(message)
    return big_endian(container.content, table.start + SAMPLE_COUNT_OFFSET, SIZE_BYTES)


def read_sound_track(container: Container, media: Box, limit: DurationLimit) -> SoundTrack:
    timing = read_timing(container.content, container.required(media, MEDIA_HEADER))
    check_timing(timing, limit)
    samples = container.required(container.required(media, MEDIA_INFORMATION), SAMPLE_TABLE)
    description = container.required(samples, SAMPLE_DESCRIPTION)
    if four_cc_at(container.content, description, SAMPLE_ENTRY_FORMAT_OFFSET) in ENCRYPTED_SAMPLE_ENTRIES:
        message = "the recording's audio is encrypted"
        raise unsupported(message)
    sampling = sampled_duration(container, container.required(samples, TIME_TO_SAMPLE), timing, limit)
    if sampling.samples != stored_samples(container, samples):
        message = "the recording's time-to-sample table does not cover exactly the samples it stores"
        raise unsupported(message)
    return SoundTrack(timing=timing, sampled_duration=sampling.duration)


def seconds_of(units: int, timescale: int) -> float:
    return units / timescale


@dataclass(frozen=True, slots=True)
class MovieSummary:
    movie_timing: Timing
    sound_tracks: tuple[SoundTrack, ...]
    has_video: bool

    @property
    def audio_seconds(self) -> float:
        return max(
            (seconds_of(track.sampled_duration, track.timing.timescale) for track in self.sound_tracks),
            default=0.0,
        )

    @property
    def seconds(self) -> float:
        candidates = [self.audio_seconds]
        timings = [self.movie_timing, *(track.timing for track in self.sound_tracks)]
        candidates.extend(
            seconds_of(timing.duration, timing.timescale) for timing in timings if timing.duration is not None
        )
        return max(candidates)


def summarise_movie(container: Container, movie: Box, limit: DurationLimit) -> MovieSummary:
    header: Box | None = None
    tracks: list[Box] = []
    for box in container.children(movie.start, movie.end):
        if box.kind == MOVIE_EXTENDS:
            message = "fragmented MP4 recordings are not supported"
            raise unsupported(message)
        if box.kind == MOVIE_HEADER:
            if header is not None:
                message = "the recording has more than one movie header"
                raise unsupported(message)
            header = box
        if box.kind == TRACK:
            tracks.append(box)
    if header is None:
        message = "the recording has no movie header"
        raise unsupported(message)
    if len(tracks) > MAX_TRACKS:
        message = f"the recording has more than {MAX_TRACKS} tracks"
        raise unsupported(message)
    movie_timing = read_timing(container.content, header)
    check_timing(movie_timing, limit)
    sound_tracks: list[SoundTrack] = []
    has_video = False
    for track in tracks:
        media = container.required(track, MEDIA)
        handler = four_cc_at(container.content, container.required(media, HANDLER), HANDLER_TYPE_OFFSET)
        if handler == SOUND_HANDLER:
            sound_tracks.append(read_sound_track(container, media, limit))
        has_video = has_video or handler == VIDEO_HANDLER
    return MovieSummary(movie_timing=movie_timing, sound_tracks=tuple(sound_tracks), has_video=has_video)


def single_movie(top_level: list[Box]) -> Box:
    if any(box.kind == MOVIE_FRAGMENT for box in top_level):
        message = "fragmented MP4 recordings are not supported"
        raise unsupported(message)
    movies = [box for box in top_level if box.kind == MOVIE]
    if len(movies) != 1:
        message = f"the recording has {len(movies)} movie boxes instead of one"
        raise unsupported(message)
    return movies[0]


def recording_format(major: bytes, *, has_video: bool) -> DocumentFormat:
    if major == QUICKTIME_BRAND:
        return DocumentFormat.MOV
    return DocumentFormat.MP4 if has_video else DocumentFormat.M4A


def is_isobmff(content: bytes) -> bool:
    return content[BRAND_BYTES : BRAND_BYTES + BRAND_BYTES] == FILE_TYPE


def probe_isobmff(content: bytes, limit: DurationLimit) -> InspectedUpload:
    container = Container(
        content=content,
        boxes=Budget(MAX_BOXES, "boxes"),
        entries=Budget(MAX_TIME_TO_SAMPLE_ENTRIES, "time-to-sample entries"),
    )
    top_level = list(container.children(0, len(content)))
    major = read_major_brand(content, top_level[0])
    summary = summarise_movie(container, single_movie(top_level), limit)
    if summary.audio_seconds <= 0:
        message = "the recording has no audio"
        raise unsupported(message)
    return InspectedUpload(
        document_format=recording_format(major, has_video=summary.has_video), duration_seconds=summary.seconds
    )
