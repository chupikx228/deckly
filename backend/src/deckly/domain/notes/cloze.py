import re
from dataclasses import dataclass
from typing import ClassVar

from deckly.domain.exceptions import InvalidClozeError
from deckly.domain.notes.fields import NoteFields, require_text
from deckly.domain.notes.note_type import NoteType
from deckly.domain.text import is_blank

CLOZE_OPENING = re.compile(r"\{\{c([0-9]+)::")
CLOZE_CLOSING = "}}"
HINT_SEPARATOR = "::"
LINE_BREAKS = frozenset({"\n", "\r", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}"})


@dataclass(frozen=True, slots=True)
class ClozeFields(NoteFields):
    note_type: ClassVar[NoteType] = NoteType.CLOZE

    text: str
    extra: str = ""

    def __post_init__(self) -> None:
        require_text(self.text, "text")
        numbers = cloze_numbers(self.text)
        if numbers != set(range(1, len(numbers) + 1)):
            message = f"cloze markers must be numbered from 1 without gaps, got {sorted(numbers)}"
            raise InvalidClozeError(message)


def cloze_numbers(text: str) -> set[int]:
    openings = list(CLOZE_OPENING.finditer(text))
    if not openings:
        message = "cloze text must contain at least one {{cN::...}} marker"
        raise InvalidClozeError(message)
    boundaries = [opening.start() for opening in openings[1:]] + [len(text)]
    max_digits = len(str(len(openings)))
    numbers: set[int] = set()
    for opening, boundary in zip(openings, boundaries, strict=True):
        closing = text.find(CLOZE_CLOSING, opening.end(), boundary)
        if closing == -1:
            message = "cloze text contains an unterminated or nested marker"
            raise InvalidClozeError(message)
        content = text[opening.end() : closing]
        if not LINE_BREAKS.isdisjoint(content):
            message = "cloze marker must not span lines"
            raise InvalidClozeError(message)
        if is_blank(content.split(HINT_SEPARATOR, 1)[0]):
            message = "cloze marker has a blank answer"
            raise InvalidClozeError(message)
        digits = opening.group(1)
        if digits.startswith("0") or len(digits) > max_digits:
            message = f"cloze number {digits[: max_digits + 1]!r} cannot be part of a gapless sequence"
            raise InvalidClozeError(message)
        numbers.add(int(digits))
    return numbers
