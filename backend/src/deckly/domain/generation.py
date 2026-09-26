from dataclasses import dataclass
from enum import StrEnum

from deckly.domain.exceptions import InvalidGenerationRequestError, UnsupportedNoteTypeError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import fields_type_for
from deckly.domain.text import is_blank


class Difficulty(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    topic: str
    language: str
    card_count: int
    difficulty: Difficulty
    note_types: tuple[NoteType, ...]
    include_images: bool
    instructions: str | None

    def __post_init__(self) -> None:
        if is_blank(self.topic):
            message = "topic must contain visible characters"
            raise InvalidGenerationRequestError(message)
        if not self.note_types:
            message = "at least one note type is required"
            raise InvalidGenerationRequestError(message)
        if len(set(self.note_types)) != len(self.note_types):
            message = f"note types must be unique, got {[str(note_type) for note_type in self.note_types]}"
            raise InvalidGenerationRequestError(message)
        for note_type in self.note_types:
            try:
                fields_type_for(note_type)
            except UnsupportedNoteTypeError as error:
                message = f"note type {note_type} cannot be generated yet"
                raise InvalidGenerationRequestError(message) from error
        if NoteType.IMAGE_OCCLUSION in self.note_types and not self.include_images:
            message = f"note type {NoteType.IMAGE_OCCLUSION} is only generated with includeImages"
            raise InvalidGenerationRequestError(message)
