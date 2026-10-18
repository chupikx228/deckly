from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from deckly.domain.document import Document, is_recording
from deckly.domain.exceptions import InvalidGenerationRequestError, UnsupportedNoteTypeError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import fields_type_for
from deckly.domain.source import SourceQuery
from deckly.domain.text import collapse_whitespace, is_blank, is_uuid_v4

UNGENERATABLE_NOTE_TYPES = frozenset({NoteType.IMAGE_OCCLUSION})
MIN_CARD_COUNT = 5
MAX_CARD_COUNT = 200


class Difficulty(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class SourceKind(StrEnum):
    TOPIC = "topic"
    DOCUMENT = "document"
    RECORDING = "recording"


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
class DocumentGenerationFingerprint:
    document_id: UUID
    language: str
    card_count: int
    difficulty: Difficulty
    note_types: tuple[NoteType, ...]
    include_images: bool
    instructions: str | None


@dataclass(frozen=True, slots=True)
class RecordingGenerationFingerprint:
    document_id: UUID
    language: str
    card_count: int
    difficulty: Difficulty
    note_types: tuple[NoteType, ...]
    include_images: bool
    instructions: str | None


type Fingerprint = GenerationFingerprint | DocumentGenerationFingerprint | RecordingGenerationFingerprint


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    topic: str | None
    language: str
    card_count: int
    difficulty: Difficulty
    note_types: tuple[NoteType, ...]
    include_images: bool
    instructions: str | None
    document_id: UUID | None = None

    def __post_init__(self) -> None:
        if (self.topic is None) == (self.document_id is None):
            message = "a request must name exactly one of a topic and a documentId"
            raise InvalidGenerationRequestError(message)
        if self.topic is not None and is_blank(self.topic):
            message = "topic must contain visible characters"
            raise InvalidGenerationRequestError(message)
        if self.document_id is not None and not is_uuid_v4(self.document_id):
            message = f"documentId {self.document_id} is not a UUID v4"
            raise InvalidGenerationRequestError(message)
        if not MIN_CARD_COUNT <= self.card_count <= MAX_CARD_COUNT:
            message = f"card count must be {MIN_CARD_COUNT} to {MAX_CARD_COUNT}, got {self.card_count}"
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

    def source_kind(self, document: Document | None) -> SourceKind:
        self._require_named(document)
        if self.document_id is None:
            return SourceKind.TOPIC
        if document is not None and is_recording(document.document_format):
            return SourceKind.RECORDING
        return SourceKind.DOCUMENT

    def source_query(self) -> SourceQuery:
        if self.topic is None:
            message = "a request that generates from a document has no topic to search for"
            raise InvalidGenerationRequestError(message)
        return SourceQuery(topic=self.topic, language=self.language)

    def fingerprint(self, document: Document | None = None) -> Fingerprint:
        self._require_named(document)
        collapsed = None if self.instructions is None else collapse_whitespace(self.instructions)
        instructions = None if collapsed is None or is_blank(collapsed) else collapsed
        note_types = tuple(sorted(self.note_types))
        language = self.language.lower()
        if self.document_id is None:
            return GenerationFingerprint(
                topic=collapse_whitespace(self.source_query().topic),
                language=language,
                card_count=self.card_count,
                difficulty=self.difficulty,
                note_types=note_types,
                include_images=self.include_images,
                instructions=instructions,
            )
        if document is None:
            message = "a request that generates from a document is fingerprinted together with that document"
            raise InvalidGenerationRequestError(message)
        fingerprint_type = (
            RecordingGenerationFingerprint
            if is_recording(document.document_format)
            else DocumentGenerationFingerprint
        )
        return fingerprint_type(
            document_id=self.document_id,
            language=language,
            card_count=self.card_count,
            difficulty=self.difficulty,
            note_types=note_types,
            include_images=self.include_images,
            instructions=instructions,
        )

    def _require_named(self, document: Document | None) -> None:
        if document is None or document.document_id == self.document_id:
            return
        message = f"document {document.document_id} is not the source this request names"
        raise InvalidGenerationRequestError(message)
