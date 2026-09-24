from collections import Counter
from dataclasses import dataclass

from deckly.domain.exceptions import DuplicateClientIdError, InvalidDeckError
from deckly.domain.notes.note import Note
from deckly.domain.text import is_blank, utf16_length

MAX_TITLE_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 500


@dataclass(frozen=True, slots=True)
class Deck:
    title: str
    description: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if is_blank(self.title):
            message = "deck title must not be blank"
            raise InvalidDeckError(message)
        if utf16_length(self.title) > MAX_TITLE_LENGTH:
            message = f"deck title exceeds {MAX_TITLE_LENGTH} characters"
            raise InvalidDeckError(message)
        if self.description is not None and utf16_length(self.description) > MAX_DESCRIPTION_LENGTH:
            message = f"deck description exceeds {MAX_DESCRIPTION_LENGTH} characters"
            raise InvalidDeckError(message)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    deck: Deck
    notes: tuple[Note, ...]

    def __post_init__(self) -> None:
        counts = Counter(note.client_id for note in self.notes)
        duplicates = sorted(str(client_id) for client_id, count in counts.items() if count > 1)
        if duplicates:
            message = f"clientId must be unique within a result, duplicated: {duplicates}"
            raise DuplicateClientIdError(message)
