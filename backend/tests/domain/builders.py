from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

import pytest

from deckly.domain.deck import Deck, GenerationResult
from deckly.domain.notes import registry
from deckly.domain.notes.basic import BasicFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source

T0 = datetime(2026, 8, 14, 10, 30, tzinfo=UTC)
JOB_ID = UUID("5f0c2f4e-2b8a-4c1e-9d7a-1f2e3d4c5b6a")
SOURCES = (Source(title="Traffic regulations", url="https://example.com/rules"),)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def client_id(number: int) -> UUID:
    return UUID(int=number, version=4)


def basic_note(number: int) -> Note:
    return Note(
        client_id=client_id(number), fields=BasicFields(front=f"front {number}", back="back"), sources=SOURCES
    )


def result_with(*notes: Note) -> GenerationResult:
    return GenerationResult(deck=Deck(title="Road signs"), notes=notes)


def unregister(monkeypatch: pytest.MonkeyPatch, note_type: NoteType) -> None:
    remaining = {
        registered: shape
        for registered, shape in registry.NOTE_FIELDS_BY_TYPE.items()
        if registered is not note_type
    }
    monkeypatch.setattr(registry, "NOTE_FIELDS_BY_TYPE", MappingProxyType(remaining))
