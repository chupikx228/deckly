from dataclasses import dataclass
from typing import ClassVar

from deckly.domain.exceptions import InvalidNoteError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.text import is_blank


@dataclass(frozen=True, slots=True)
class NoteFields:
    note_type: ClassVar[NoteType]

    @property
    def referenced_image_ids(self) -> frozenset[str]:
        return frozenset()


def require_text(value: str, field: str) -> None:
    if is_blank(value):
        message = f"{field} must not be blank"
        raise InvalidNoteError(message)


def require_bool(value: object, field: str) -> None:
    if not isinstance(value, bool):
        message = f"{field} must be a boolean, got {type(value).__name__}"
        raise InvalidNoteError(message)
