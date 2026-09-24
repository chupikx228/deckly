from dataclasses import dataclass
from typing import ClassVar

from deckly.domain.exceptions import InvalidNoteError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.text import is_blank


@dataclass(frozen=True, slots=True)
class NoteFields:
    note_type: ClassVar[NoteType]


def require_text(value: str, field: str) -> None:
    if is_blank(value):
        message = f"{field} must not be blank"
        raise InvalidNoteError(message)
