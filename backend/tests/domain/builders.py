from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType
from uuid import UUID

import pytest

from deckly.domain.deck import Deck, GenerationResult
from deckly.domain.media import Media, MediaKind
from deckly.domain.notes import registry
from deckly.domain.notes.basic import (
    BasicFields,
    BasicOptionalReversedFields,
    BasicReversedFields,
    BasicTypeInFields,
)
from deckly.domain.notes.cloze import ClozeFields
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.image_occlusion import ImageOcclusionFields, OcclusionRegion
from deckly.domain.notes.multiple_choice import MultipleChoiceFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source

T0 = datetime(2026, 8, 14, 10, 30, tzinfo=UTC)
JOB_ID = UUID("5f0c2f4e-2b8a-4c1e-9d7a-1f2e3d4c5b6a")
SOURCES = (Source(title="Traffic regulations", url="https://example.com/rules"),)
IMAGE_ID = UUID("3d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a")
MOSCOW = timezone(timedelta(hours=3))

IMAGE = Media(
    media_id=IMAGE_ID,
    kind=MediaKind.IMAGE,
    url="https://example.com/sign.png",
    license="CC-BY-4.0",
    alt="Warning sign",
    width=640,
    height=480,
)
AUDIO = Media(
    media_id=UUID("6e5d4c3b-2a19-4f8e-9d7c-6b5a4f3e2d1c"),
    kind=MediaKind.AUDIO,
    url="https://example.com/sign.mp3",
    license="CC0-1.0",
)
FIELDS_BY_TYPE: dict[NoteType, NoteFields] = {
    NoteType.BASIC: BasicFields(front="Red triangle?", back="A warning sign"),
    NoteType.BASIC_REVERSED: BasicReversedFields(front="Stop", back="Octagon"),
    NoteType.BASIC_OPTIONAL_REVERSED: BasicOptionalReversedFields(
        front="Yield", back="Triangle", add_reverse=True
    ),
    NoteType.BASIC_TYPE_IN: BasicTypeInFields(front="Speed limit in towns", back="60"),
    NoteType.CLOZE: ClozeFields(text="A {{c1::red}} border means {{c2::prohibition}}", extra="Mostly"),
    NoteType.MULTIPLE_CHOICE: MultipleChoiceFields(
        question="Which shape is a stop sign?", answer="Octagon", distractors=("Circle", "Square")
    ),
    NoteType.IMAGE_OCCLUSION: ImageOcclusionFields(
        image_id=str(IMAGE_ID),
        regions=(OcclusionRegion(1, 0.1, 0.1, 0.2, 0.2), OcclusionRegion(3, 0.5, 0.5, 0.5, 0.5)),
    ),
}


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


def note_of(number: int, note_type: NoteType) -> Note:
    media = (IMAGE, AUDIO) if note_type is NoteType.IMAGE_OCCLUSION else ()
    return Note(
        client_id=client_id(number),
        fields=FIELDS_BY_TYPE[note_type],
        sources=(
            Source(
                title="Traffic code", url="https://example.com/code", retrieved_at=at(20).astimezone(MOSCOW)
            ),
            *SOURCES,
        ),
        media=media,
        tags=("signs",),
    )


FULL_RESULT = GenerationResult(
    deck=Deck(title="Road signs", description="Warning and prohibitory signs", tags=("driving", "signs")),
    notes=tuple(note_of(number, note_type) for number, note_type in enumerate(FIELDS_BY_TYPE, start=1)),
)


def unregister(monkeypatch: pytest.MonkeyPatch, note_type: NoteType) -> None:
    remaining = {
        registered: shape
        for registered, shape in registry.NOTE_FIELDS_BY_TYPE.items()
        if registered is not note_type
    }
    monkeypatch.setattr(registry, "NOTE_FIELDS_BY_TYPE", MappingProxyType(remaining))
