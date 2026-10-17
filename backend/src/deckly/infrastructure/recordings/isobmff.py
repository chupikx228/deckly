import struct
from collections.abc import Sequence
from dataclasses import dataclass

from deckly.application.ports import InspectedUpload
from deckly.domain.document import DocumentFormat
from deckly.infrastructure.recordings.binary import big_endian
from deckly.infrastructure.recordings.boxes import (
    FULL_BOX_PREFIX_BYTES,
    SIZE_BYTES,
    VERSION_1,
    Box,
    Budget,
    Container,
    Sampling,
    field,
    unsupported,
    version_of,
)
from deckly.infrastructure.recordings.duration import DurationLimit, PacketFloor
from deckly.infrastructure.recordings.esds import aac_configuration, decoder_configuration, first_sample_entry
from deckly.infrastructure.recordings.fragments import (
    MOVIE_FRAGMENT,
    FragmentedTrack,
    FragmentWalk,
    fragment_duration,
    track_extends_defaults,
)

BITS_PER_BYTE = 8
MAX_BOXES = 4096
MAX_FRAGMENT_BOXES = 16_384
MAX_TRACKS = 64
MAX_TIME_TO_SAMPLE_ENTRIES = 1_048_576
TIME_TO_SAMPLE_ENTRY_BYTES = 8
BRAND_BYTES = 4
FILE_TYPE_PREFIX_BYTES = 8
TRACK_ID_OFFSET_V0 = 12
TRACK_ID_OFFSET_V1 = 20
FILE_TYPE = b"ftyp"
MOVIE = b"moov"
MOVIE_HEADER = b"mvhd"
MOVIE_EXTENDS = b"mvex"
MEDIA_DATA = b"mdat"
TRACK = b"trak"
TRACK_HEADER = b"tkhd"
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
FRAGMENT_LEVEL_BOXES = frozenset({MOVIE_FRAGMENT, MEDIA_DATA})
HANDLER_TYPE_OFFSET = 8
SAMPLE_ENTRY_FORMAT_OFFSET = 12
AAC_SAMPLE_ENTRY = b"mp4a"


@dataclass(frozen=True, slots=True)
class Timing:
    timescale: int
    duration: int | None


@dataclass(frozen=True, slots=True)
class SoundTrack:
    timing: Timing
    sampled_duration: int
    packets: int = 0
    floor: PacketFloor | None = None

    @property
    def seconds(self) -> float:
        sampled = seconds_of(self.sampled_duration, self.timing.timescale)
        return sampled if self.floor is None else max(sampled, self.floor.seconds(self.packets))


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


def packet_floor(container: Container, description: Box) -> PacketFloor | None:
    entry = first_sample_entry(container, description)
    configuration = decoder_configuration(container, entry)
    if configuration is None:
        if entry.kind == AAC_SAMPLE_ENTRY:
            message = "the recording's AAC audio has no decoder configuration"
            raise unsupported(message)
        return None
    aac = aac_configuration(container.content, configuration)
    return None if aac is None else aac.floor


def read_sound_track(container: Container, media: Box, limit: DurationLimit) -> SoundTrack:
    timing = read_timing(container.content, container.required(media, MEDIA_HEADER))
    check_timing(timing, limit)
    samples = container.required(container.required(media, MEDIA_INFORMATION), SAMPLE_TABLE)
    description = container.required(samples, SAMPLE_DESCRIPTION)
    if four_cc_at(container.content, description, SAMPLE_ENTRY_FORMAT_OFFSET) in ENCRYPTED_SAMPLE_ENTRIES:
        message = "the recording's audio is encrypted"
        raise unsupported(message)
    floor = packet_floor(container, description)
    sampling = sampled_duration(container, container.required(samples, TIME_TO_SAMPLE), timing, limit)
    if sampling.samples != stored_samples(container, samples):
        message = "the recording's time-to-sample table does not cover exactly the samples it stores"
        raise unsupported(message)
    if floor is not None:
        limit.check_packets(sampling.samples, floor)
    return SoundTrack(
        timing=timing, sampled_duration=sampling.duration, packets=sampling.samples, floor=floor
    )


def seconds_of(units: int, timescale: int) -> float:
    return units / timescale


@dataclass(frozen=True, slots=True)
class MovieSummary:
    movie_timing: Timing
    sound_tracks: tuple[SoundTrack, ...]
    has_video: bool
    fragment_timing: Timing | None = None

    @property
    def audio_seconds(self) -> float:
        return max((track.seconds for track in self.sound_tracks), default=0.0)

    @property
    def seconds(self) -> float:
        candidates = [self.audio_seconds]
        timings = [self.movie_timing, *(track.timing for track in self.sound_tracks)]
        if self.fragment_timing is not None:
            timings.append(self.fragment_timing)
        candidates.extend(
            seconds_of(timing.duration, timing.timescale) for timing in timings if timing.duration is not None
        )
        return max(candidates)


@dataclass(frozen=True, slots=True)
class MovieContents:
    header: Box
    tracks: tuple[Box, ...]
    extends: Box | None


@dataclass(frozen=True, slots=True)
class ReadTrack:
    box: Box
    handler: bytes
    sound: SoundTrack | None


def movie_contents(container: Container, movie: Box) -> MovieContents:
    children = list(container.children(movie.start, movie.end))
    headers = [box for box in children if box.kind == MOVIE_HEADER]
    if not headers:
        message = "the recording has no movie header"
        raise unsupported(message)
    if len(headers) > 1:
        message = "the recording has more than one movie header"
        raise unsupported(message)
    extends = [box for box in children if box.kind == MOVIE_EXTENDS]
    if len(extends) > 1:
        message = "the recording has more than one movie extends box"
        raise unsupported(message)
    tracks = tuple(box for box in children if box.kind == TRACK)
    if len(tracks) > MAX_TRACKS:
        message = f"the recording has more than {MAX_TRACKS} tracks"
        raise unsupported(message)
    return MovieContents(header=headers[0], tracks=tracks, extends=extends[0] if extends else None)


def read_track(container: Container, track: Box, limit: DurationLimit) -> ReadTrack:
    media = container.required(track, MEDIA)
    handler = four_cc_at(container.content, container.required(media, HANDLER), HANDLER_TYPE_OFFSET)
    sound = read_sound_track(container, media, limit) if handler == SOUND_HANDLER else None
    return ReadTrack(box=track, handler=handler, sound=sound)


def read_track_id(content: bytes, track_header: Box) -> int:
    offset = TRACK_ID_OFFSET_V1 if version_of(content, track_header) == VERSION_1 else TRACK_ID_OFFSET_V0
    return field(content, track_header, offset, SIZE_BYTES)


def tracks_by_id(container: Container, tracks: Sequence[ReadTrack]) -> dict[int, ReadTrack]:
    by_id: dict[int, ReadTrack] = {}
    for track in tracks:
        track_id = read_track_id(container.content, container.required(track.box, TRACK_HEADER))
        if track_id in by_id:
            message = f"the recording has more than one track with id {track_id}"
            raise unsupported(message)
        by_id[track_id] = track
    return by_id


def unfragmented_sound(tracks: Sequence[ReadTrack]) -> tuple[SoundTrack, ...]:
    return tuple(sound for sound in (track.sound for track in tracks) if sound is not None)


@dataclass(frozen=True, slots=True)
class Fragmented:
    extends: Box
    fragments: tuple[Box, ...]


def fragmented_sound(
    container: Container, fragmented: Fragmented, tracks: Sequence[ReadTrack], limit: DurationLimit
) -> tuple[SoundTrack, ...]:
    defaults = track_extends_defaults(container, fragmented.extends)
    by_id = tracks_by_id(container, tracks)
    sounds = {
        track_id: sound
        for track_id, sound in ((key, track.sound) for key, track in by_id.items())
        if sound is not None
    }
    if any(sound.floor is None for sound in sounds.values()):
        message = "a fragmented recording's audio must be AAC"
        raise unsupported(message)
    walk = FragmentWalk(
        container=container,
        sound={
            track_id: FragmentedTrack(
                timescale=sound.timing.timescale, default_duration=defaults.get(track_id), floor=sound.floor
            )
            for track_id, sound in sounds.items()
        },
        others=frozenset(by_id.keys() - sounds.keys()),
        limit=limit,
    )
    totals = walk.samplings(
        fragmented.fragments,
        {
            track_id: Sampling(duration=sound.sampled_duration, samples=sound.packets)
            for track_id, sound in sounds.items()
        },
    )
    return tuple(
        SoundTrack(
            timing=sound.timing,
            sampled_duration=totals[track_id].duration,
            packets=totals[track_id].samples,
            floor=sound.floor,
        )
        for track_id, sound in sounds.items()
    )


def fragment_timing(
    container: Container, extends: Box, movie_timing: Timing, limit: DurationLimit
) -> Timing | None:
    duration = fragment_duration(container, extends)
    if duration is None:
        return None
    timing = Timing(timescale=movie_timing.timescale, duration=duration)
    check_timing(timing, limit)
    return timing


def summarise_movie(
    container: Container, movie: Box, fragments: tuple[Box, ...], limit: DurationLimit
) -> MovieSummary:
    contents = movie_contents(container, movie)
    if fragments and contents.extends is None:
        message = "the recording has movie fragments but no movie extends box"
        raise unsupported(message)
    movie_timing = read_timing(container.content, contents.header)
    check_timing(movie_timing, limit)
    tracks = tuple(read_track(container, track, limit) for track in contents.tracks)
    has_video = any(track.handler == VIDEO_HANDLER for track in tracks)
    if contents.extends is None:
        return MovieSummary(
            movie_timing=movie_timing, sound_tracks=unfragmented_sound(tracks), has_video=has_video
        )
    return MovieSummary(
        movie_timing=movie_timing,
        sound_tracks=fragmented_sound(container, Fragmented(contents.extends, fragments), tracks, limit),
        has_video=has_video,
        fragment_timing=fragment_timing(container, contents.extends, movie_timing, limit),
    )


def top_level_boxes(container: Container) -> list[Box]:
    boxes: list[Box] = []
    end = len(container.content)
    position = 0
    while position < end:
        box = container.box_at(position, end)
        budget = container.fragment_boxes if box.kind in FRAGMENT_LEVEL_BOXES else container.boxes
        budget.spend()
        boxes.append(box)
        position = box.end
    return boxes


def single_movie(top_level: list[Box]) -> Box:
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
        fragment_boxes=Budget(MAX_FRAGMENT_BOXES, "fragment boxes"),
    )
    top_level = top_level_boxes(container)
    major = read_major_brand(content, top_level[0])
    fragments = tuple(box for box in top_level if box.kind == MOVIE_FRAGMENT)
    summary = summarise_movie(container, single_movie(top_level), fragments, limit)
    if summary.audio_seconds <= 0:
        message = "the recording has no audio"
        raise unsupported(message)
    return InspectedUpload(
        document_format=recording_format(major, has_video=summary.has_video), duration_seconds=summary.seconds
    )
