from dataclasses import dataclass
from enum import StrEnum

from deckly.domain.exceptions import InvalidGenerationRequestError, UnsupportedNoteTypeError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import fields_type_for
from deckly.domain.text import collapse_whitespace, is_blank

UNGENERATABLE_NOTE_TYPES = frozenset({NoteType.IMAGE_OCCLUSION})


class Difficulty(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


@dataclass(frozen=True, slots=True)
class GenerationFingerprint:
    topic: str
    language: str
    card_count: int
    difficulty: Difficulty
    note_types: tuple[NoteType, ...]
    include_images: bool
    instructions: str | None


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
        ungeneratable = [
            str(note_type) for note_type in self.note_types if note_type in UNGENERATABLE_NOTE_TYPES
        ]
        if ungeneratable:
            message = f"note types {ungeneratable} are not generated yet"
            raise InvalidGenerationRequestError(message)

    def fingerprint(self) -> GenerationFingerprint:
        instructions = None if self.instructions is None else collapse_whitespace(self.instructions)
        return GenerationFingerprint(
            topic=collapse_whitespace(self.topic),
            language=self.language.lower(),
            card_count=self.card_count,
            difficulty=self.difficulty,
            note_types=tuple(sorted(self.note_types)),
            include_images=self.include_images,
            instructions=None if instructions is None or is_blank(instructions) else instructions,
        )
