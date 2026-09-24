from dataclasses import dataclass
from uuid import UUID

from deckly.domain.exceptions import InvalidNoteError, UnsupportedNoteTypeError
from deckly.domain.media import Media
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE
from deckly.domain.source import Source
from deckly.domain.text import is_uuid_v4


@dataclass(frozen=True, slots=True)
class Note:
    client_id: UUID
    fields: NoteFields
    media: tuple[Media, ...] = ()
    sources: tuple[Source, ...] = ()
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_uuid_v4(self.client_id):
            message = f"clientId {self.client_id} is not a UUID v4"
            raise InvalidNoteError(message)
        if type(self.fields) not in NOTE_FIELDS_BY_TYPE.values():
            message = f"{type(self.fields).__name__} is not a registered note field shape"
            raise UnsupportedNoteTypeError(message)

    @property
    def note_type(self) -> NoteType:
        return self.fields.note_type
