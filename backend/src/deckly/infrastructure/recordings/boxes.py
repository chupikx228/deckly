from collections.abc import Iterator
from dataclasses import dataclass

from deckly.domain.exceptions import UnsupportedDocumentError
from deckly.infrastructure.recordings.binary import big_endian

SIZE_BYTES = 4
LARGE_SIZE_BYTES = 8
BOX_HEADER_BYTES = 8
LARGE_BOX_HEADER_BYTES = 16
FULL_BOX_PREFIX_BYTES = 4
FLAGS_OFFSET = 1
FLAGS_BYTES = 3
SIZE_TO_END = 0
LARGE_SIZE = 1
VERSION_1 = 1


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


@dataclass(frozen=True, slots=True)
class Sampling:
    duration: int
    samples: int

    def plus(self, other: "Sampling") -> "Sampling":
        return Sampling(duration=self.duration + other.duration, samples=self.samples + other.samples)


class Budget:
    def __init__(self, allowance: int, subject: str) -> None:
        self._remaining = allowance
        self._subject = subject

    def spend(self, amount: int = 1) -> None:
        self._remaining -= amount
        if self._remaining < 0:
            message = f"the recording has too many {self._subject}"
            raise unsupported(message)


def require_size(box: Box, size: int) -> None:
    if box.size < size:
        message = f"the recording's {box.kind!r} box is too short"
        raise unsupported(message)


def field(content: bytes, box: Box, offset: int, size: int) -> int:
    require_size(box, offset + size)
    return big_endian(content, box.start + offset, size)


def version_of(content: bytes, box: Box) -> int:
    require_size(box, FULL_BOX_PREFIX_BYTES)
    return content[box.start]


def flags_of(content: bytes, box: Box) -> int:
    return field(content, box, FLAGS_OFFSET, FLAGS_BYTES)


@dataclass(frozen=True, slots=True)
class Container:
    content: bytes
    boxes: Budget
    entries: Budget
    fragment_boxes: Budget

    def children(self, start: int, end: int, budget: Budget | None = None) -> Iterator[Box]:
        spent = self.boxes if budget is None else budget
        position = start
        while position < end:
            spent.spend()
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

    def single(self, parent: Box, kind: bytes, budget: Budget | None = None) -> Box | None:
        found = [box for box in self.children(parent.start, parent.end, budget) if box.kind == kind]
        if len(found) > 1:
            message = f"the recording has more than one {kind!r} box inside {parent.kind!r}"
            raise unsupported(message)
        return found[0] if found else None

    def required(self, parent: Box, kind: bytes, budget: Budget | None = None) -> Box:
        box = self.single(parent, kind, budget)
        if box is None:
            message = f"the recording has no {kind!r} box inside {parent.kind!r}"
            raise unsupported(message)
        return box
