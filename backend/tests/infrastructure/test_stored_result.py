import json
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from deckly.domain.deck import GenerationResult
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source
from deckly.infrastructure.stored_result import dump_result, load_result
from tests.domain.builders import FULL_RESULT, at, basic_note, note_of, result_with

GOLDEN_RESULT = GenerationResult(
    deck=FULL_RESULT.deck,
    notes=(note_of(1, NoteType.BASIC), note_of(7, NoteType.IMAGE_OCCLUSION)),
)
SOURCES_DOCUMENT: list[dict[str, object]] = [
    {"title": "Traffic code", "url": "https://example.com/code", "retrieved_at": "2026-08-14T10:30:20Z"},
    {"title": "Traffic regulations", "url": "https://example.com/rules", "retrieved_at": None},
]
GOLDEN_DOCUMENT: dict[str, object] = {
    "deck": {
        "title": "Road signs",
        "description": "Warning and prohibitory signs",
        "tags": ["driving", "signs"],
    },
    "notes": [
        {
            "client_id": "00000000-0000-4000-8000-000000000001",
            "note_type": "basic",
            "fields": {"front": "Red triangle?", "back": "A warning sign"},
            "sources": SOURCES_DOCUMENT,
            "media": [],
            "tags": ["signs"],
        },
        {
            "client_id": "00000000-0000-4000-8000-000000000007",
            "note_type": "image_occlusion",
            "fields": {
                "image_id": "3d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a",
                "regions": [
                    {"ordinal": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
                    {"ordinal": 3, "x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5},
                ],
                "extra": "",
            },
            "sources": SOURCES_DOCUMENT,
            "media": [
                {
                    "media_id": "3d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a",
                    "kind": "image",
                    "url": "https://example.com/sign.png",
                    "license": "CC-BY-4.0",
                    "alt": "Warning sign",
                    "width": 640,
                    "height": 480,
                },
                {
                    "media_id": "6e5d4c3b-2a19-4f8e-9d7c-6b5a4f3e2d1c",
                    "kind": "audio",
                    "url": "https://example.com/sign.mp3",
                    "license": "CC0-1.0",
                    "alt": None,
                    "width": None,
                    "height": None,
                },
            ],
            "tags": ["signs"],
        },
    ],
}

DECK: dict[str, object] = {"title": "Road signs", "description": None, "tags": []}
SOURCE: dict[str, object] = {
    "title": "Traffic regulations",
    "url": "https://example.com/rules",
    "retrieved_at": None,
}
BASIC_NOTE: dict[str, object] = {
    "client_id": "00000000-0000-4000-8000-000000000001",
    "note_type": "basic",
    "fields": {"front": "Red triangle?", "back": "A warning sign"},
    "sources": [SOURCE],
    "media": [],
    "tags": [],
}
UNLICENSED_IMAGE: dict[str, object] = {
    "media_id": "3d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a",
    "kind": "image",
    "url": "https://example.com/sign.png",
    "license": " ",
    "alt": "Warning sign",
    "width": None,
    "height": None,
}

BLANK_TITLE_DECK: dict[str, object] = {**DECK, "title": " "}

SUB_MINUTE_OFFSET = timezone(timedelta(hours=5, minutes=30, seconds=15))


def with_notes(*notes: dict[str, object]) -> dict[str, object]:
    return {"deck": DECK, "notes": list(notes)}


def test_stored_format_is_pinned() -> None:
    assert dump_result(GOLDEN_RESULT) == GOLDEN_DOCUMENT


def test_pinned_document_loads_back_into_the_same_result() -> None:
    assert load_result(GOLDEN_DOCUMENT) == GOLDEN_RESULT


def test_retrieved_at_with_a_sub_minute_utc_offset_keeps_its_instant() -> None:
    retrieved_at = at(20).astimezone(SUB_MINUTE_OFFSET)
    source = Source(title="Traffic code", url="https://example.com/code", retrieved_at=retrieved_at)

    [restored] = load_result(dump_result(result_with(replace(basic_note(1), sources=(source,))))).notes

    assert restored.sources[0].retrieved_at == retrieved_at


@pytest.mark.parametrize(
    "result", [FULL_RESULT, result_with()], ids=["every note type", "a bare deck and no notes"]
)
def test_result_survives_a_trip_through_json_text(result: GenerationResult) -> None:
    assert load_result(json.loads(json.dumps(dump_result(result)))) == result


CORRUPT_DOCUMENTS: dict[str, object] = {
    "not an object": ["deck", "notes"],
    "no notes": {"deck": DECK},
    "an unknown top-level key": {**with_notes(BASIC_NOTE), "version": 2},
    "an unknown note key": with_notes({**BASIC_NOTE, "rating": 5}),
    "an unknown note type": with_notes({**BASIC_NOTE, "note_type": "flashcard"}),
    "fields of another note type": with_notes({**BASIC_NOTE, "fields": {"text": "{{c1::Paris}}"}}),
    "a cloze without markers": with_notes({**BASIC_NOTE, "note_type": "cloze", "fields": {"text": "Paris"}}),
    "a note without sources": with_notes({**BASIC_NOTE, "sources": []}),
    "an unlicensed image": with_notes({**BASIC_NOTE, "media": [UNLICENSED_IMAGE]}),
    "a client id that is not a uuid": with_notes({**BASIC_NOTE, "client_id": "note-1"}),
    "duplicate client ids": with_notes(BASIC_NOTE, BASIC_NOTE),
    "a blank deck title": {"deck": BLANK_TITLE_DECK, "notes": [BASIC_NOTE]},
}


@pytest.mark.parametrize("document", CORRUPT_DOCUMENTS.values(), ids=CORRUPT_DOCUMENTS.keys())
def test_corrupt_document_is_rejected_as_a_value_error(document: object) -> None:
    with pytest.raises(ValueError, match=r".+"):
        load_result(document)
