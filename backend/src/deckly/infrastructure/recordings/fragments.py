import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from deckly.infrastructure.recordings.boxes import (
    FULL_BOX_PREFIX_BYTES,
    LARGE_SIZE_BYTES,
    SIZE_BYTES,
    VERSION_1,
    Box,
    Container,
    Sampling,
    field,
    flags_of,
    require_size,
    unsupported,
    version_of,
)
from deckly.infrastructure.recordings.duration import DurationLimit, PacketFloor

MOVIE_FRAGMENT = b"moof"
TRACK_FRAGMENT = b"traf"
TRACK_FRAGMENT_HEADER = b"tfhd"
TRACK_RUN = b"trun"
TRACK_EXTENDS = b"trex"
MOVIE_EXTENDS_HEADER = b"mehd"
TRACK_ID_OFFSET = 4
TRACK_EXTENDS_DURATION_OFFSET = 12
FRAGMENT_HEADER_FIELDS_OFFSET = 8
RUN_SAMPLE_COUNT_OFFSET = 4
RUN_FIELDS_OFFSET = 8
BASE_DATA_OFFSET_PRESENT = 0x000001
SAMPLE_DESCRIPTION_INDEX_PRESENT = 0x000002
DEFAULT_SAMPLE_DURATION_PRESENT = 0x000008
DATA_OFFSET_PRESENT = 0x000001
FIRST_SAMPLE_FLAGS_PRESENT = 0x000004
SAMPLE_DURATION_PRESENT = 0x000100
SAMPLE_FIELD_FLAGS = (0x000100, 0x000200, 0x000400, 0x000800)


@dataclass(frozen=True, slots=True)
class FragmentedTrack:
    timescale: int
    default_duration: int | None
    floor: PacketFloor | None


@dataclass(frozen=True, slots=True)
class FragmentHeader:
    track_id: int
    default_duration: int | None


def track_extends_defaults(container: Container, extends: Box) -> dict[int, int]:
    defaults: dict[int, int] = {}
    for box in container.children(extends.start, extends.end):
        if box.kind != TRACK_EXTENDS:
            continue
        track_id = field(container.content, box, TRACK_ID_OFFSET, SIZE_BYTES)
        if track_id in defaults:
            message = f"the recording has more than one track extends box for track {track_id}"
            raise unsupported(message)
        defaults[track_id] = field(container.content, box, TRACK_EXTENDS_DURATION_OFFSET, SIZE_BYTES)
    return defaults


def fragment_duration(container: Container, extends: Box) -> int | None:
    header = container.single(extends, MOVIE_EXTENDS_HEADER)
    if header is None:
        return None
    size = LARGE_SIZE_BYTES if version_of(container.content, header) == VERSION_1 else SIZE_BYTES
    return field(container.content, header, FULL_BOX_PREFIX_BYTES, size)


def read_fragment_header(content: bytes, header: Box) -> FragmentHeader:
    flags = flags_of(content, header)
    offset = FRAGMENT_HEADER_FIELDS_OFFSET
    if flags & BASE_DATA_OFFSET_PRESENT:
        offset += LARGE_SIZE_BYTES
    if flags & SAMPLE_DESCRIPTION_INDEX_PRESENT:
        offset += SIZE_BYTES
    default_duration = (
        field(content, header, offset, SIZE_BYTES) if flags & DEFAULT_SAMPLE_DURATION_PRESENT else None
    )
    return FragmentHeader(
        track_id=field(content, header, TRACK_ID_OFFSET, SIZE_BYTES), default_duration=default_duration
    )


def run_sampling(container: Container, run: Box, default_duration: int | None) -> Sampling:
    content = container.content
    flags = flags_of(content, run)
    count = field(content, run, RUN_SAMPLE_COUNT_OFFSET, SIZE_BYTES)
    offset = RUN_FIELDS_OFFSET
    if flags & DATA_OFFSET_PRESENT:
        offset += SIZE_BYTES
    if flags & FIRST_SAMPLE_FLAGS_PRESENT:
        offset += SIZE_BYTES
    stride = SIZE_BYTES * sum(1 for sample_field in SAMPLE_FIELD_FLAGS if flags & sample_field)
    require_size(run, offset + count * stride)
    container.entries.spend(count)
    if flags & SAMPLE_DURATION_PRESENT:
        start = run.start + offset
        samples = memoryview(content)[start : start + count * stride]
        layout = f">I{stride - SIZE_BYTES}x"
        duration = sum(int(sample_duration) for (sample_duration,) in struct.iter_unpack(layout, samples))
        return Sampling(duration=duration, samples=count)
    if default_duration is None:
        message = "the recording's track run has samples without a duration"
        raise unsupported(message)
    return Sampling(duration=count * default_duration, samples=count)


@dataclass(frozen=True, slots=True)
class FragmentWalk:
    container: Container
    sound: Mapping[int, FragmentedTrack]
    others: frozenset[int]
    limit: DurationLimit

    def samplings(
        self, fragments: Iterable[Box], unfragmented: Mapping[int, Sampling]
    ) -> dict[int, Sampling]:
        totals = dict(unfragmented)
        budget = self.container.fragment_boxes
        for fragment in fragments:
            for track_fragment in self.container.children(fragment.start, fragment.end, budget):
                if track_fragment.kind == TRACK_FRAGMENT:
                    self._add(track_fragment, totals)
        return totals

    def _add(self, track_fragment: Box, totals: dict[int, Sampling]) -> None:
        container = self.container
        budget = container.fragment_boxes
        header = read_fragment_header(
            container.content, container.required(track_fragment, TRACK_FRAGMENT_HEADER, budget)
        )
        if header.track_id in self.others:
            return
        track = self.sound.get(header.track_id)
        if track is None:
            message = (
                f"a fragment of the recording refers to track {header.track_id}, which it does not declare"
            )
            raise unsupported(message)
        default_duration = (
            track.default_duration if header.default_duration is None else header.default_duration
        )
        for run in container.children(track_fragment.start, track_fragment.end, budget):
            if run.kind == TRACK_RUN:
                total = totals[header.track_id].plus(run_sampling(container, run, default_duration))
                totals[header.track_id] = total
                self.limit.check(total.duration, track.timescale)
                if track.floor is not None:
                    self.limit.check_packets(total.samples, track.floor)
