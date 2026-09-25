from dataclasses import replace
from datetime import UTC
from functools import cache
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter

from deckly.domain.deck import Deck, GenerationResult
from deckly.domain.exceptions import DomainError
from deckly.domain.media import Media
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import fields_type_for
from deckly.domain.source import Source


@cache
def fields_adapter(note_type: NoteType) -> TypeAdapter[NoteFields]:
    return TypeAdapter(fields_type_for(note_type))


def retrieved_in_utc(source: Source) -> Source:
    if source.retrieved_at is None:
        return source
    return replace(source, retrieved_at=source.retrieved_at.astimezone(UTC))


class StoredNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: UUID
    note_type: NoteType
    fields: dict[str, object]
    sources: tuple[Source, ...]
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
