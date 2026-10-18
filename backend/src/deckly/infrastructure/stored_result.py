from dataclasses import replace
from datetime import UTC
from functools import cache
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Discriminator, Tag, TypeAdapter

from deckly.domain.deck import Deck, GenerationResult
from deckly.domain.exceptions import DomainError
from deckly.domain.media import Media
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import fields_type_for
from deckly.domain.source import DocumentSource, Source, TranscriptSource, WebSource

WEB_SOURCE_TAG = "web"
DOCUMENT_SOURCE_TAG = "document"
TRANSCRIPT_SOURCE_TAG = "transcript"
DOCUMENT_SOURCE_KEY = "document_id"
TRANSCRIPT_SOURCE_KEY = "start_ms"


def stored_source_tag(value: object) -> str:
    if isinstance(value, TranscriptSource) or (isinstance(value, dict) and TRANSCRIPT_SOURCE_KEY in value):
        return TRANSCRIPT_SOURCE_TAG
    if isinstance(value, DocumentSource) or (isinstance(value, dict) and DOCUMENT_SOURCE_KEY in value):
        return DOCUMENT_SOURCE_TAG
    return WEB_SOURCE_TAG


type StoredSource = Annotated[
    Annotated[WebSource, Tag(WEB_SOURCE_TAG)]
    | Annotated[DocumentSource, Tag(DOCUMENT_SOURCE_TAG)]
    | Annotated[TranscriptSource, Tag(TRANSCRIPT_SOURCE_TAG)],
    Discriminator(stored_source_tag),
]


@cache
def fields_adapter(note_type: NoteType) -> TypeAdapter[NoteFields]:
    return TypeAdapter(fields_type_for(note_type))


def retrieved_in_utc(source: Source) -> Source:
    if isinstance(source, WebSource) and source.retrieved_at is not None:
        return replace(source, retrieved_at=source.retrieved_at.astimezone(UTC))
    return source


class StoredNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: UUID
    note_type: NoteType
    fields: dict[str, object]
    sources: tuple[StoredSource, ...]
    media: tuple[Media, ...]
    tags: tuple[str, ...]

    @classmethod
    def from_note(cls, note: Note) -> Self:
        return cls(
            client_id=note.client_id,
            note_type=note.note_type,
            fields=fields_adapter(note.note_type).dump_python(note.fields, mode="json"),
            sources=tuple(retrieved_in_utc(source) for source in note.sources),
            media=note.media,
            tags=note.tags,
        )

    def to_note(self) -> Note:
        return Note(
            client_id=self.client_id,
            fields=fields_adapter(self.note_type).validate_python(self.fields),
            sources=self.sources,
            media=self.media,
            tags=self.tags,
        )


class StoredResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deck: Deck
    notes: tuple[StoredNote, ...]


def dump_result(result: GenerationResult) -> dict[str, object]:
    stored = StoredResult(deck=result.deck, notes=tuple(StoredNote.from_note(note) for note in result.notes))
    return stored.model_dump(mode="json")


def load_result(payload: object) -> GenerationResult:
    try:
        stored = StoredResult.model_validate(payload)
        return GenerationResult(deck=stored.deck, notes=tuple(note.to_note() for note in stored.notes))
    except DomainError as error:
        raise ValueError(str(error)) from error
