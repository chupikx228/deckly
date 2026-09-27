from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from deckly.domain.exceptions import InvalidGenerationRequestError, UnsupportedNoteTypeError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import fields_type_for
from deckly.domain.text import is_blank

UNREGENERATABLE_NOTE_TYPES = frozenset({NoteType.IMAGE_OCCLUSION})


class RejectionReason(StrEnum):
    TOO_EASY = "too_easy"
    TOO_HARD = "too_hard"
    INCORRECT = "incorrect"
    DUPLICATE = "duplicate"
    OFF_TOPIC = "off_topic"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class RegenerationRequest:
    topic: str
    language: str
    note_type: NoteType
    rejected_fields: Mapping[str, object]
    reason: RejectionReason

    def __post_init__(self) -> None:
        if is_blank(self.topic):
            message = "topic must contain visible characters"
            raise InvalidGenerationRequestError(message)
        if self.note_type in UNREGENERATABLE_NOTE_TYPES:
            message = f"note type {self.note_type} is built on an image the request does not carry"
            raise InvalidGenerationRequestError(message)
        try:
            fields_type_for(self.note_type)
        except UnsupportedNoteTypeError as error:
            message = f"note type {self.note_type} cannot be regenerated"
            raise InvalidGenerationRequestError(message) from error
