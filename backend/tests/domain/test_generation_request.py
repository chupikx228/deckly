from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Unpack
from uuid import UUID

import pytest

from deckly.domain.document import RECORDING_FORMATS, Document, DocumentFormat
from deckly.domain.exceptions import InvalidGenerationRequestError
from deckly.domain.generation import (
    MAX_CARD_COUNT,
    MIN_CARD_COUNT,
    UNGENERATABLE_NOTE_TYPES,
    Difficulty,
    DocumentGenerationFingerprint,
    Fingerprint,
    GenerationFingerprint,
    GenerationRequest,
    RecordingGenerationFingerprint,
    SourceKind,
)
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE
from deckly.domain.source import SourceQuery
from tests.domain.builders import RequestChanges, unregister


def request_with(
    *note_types: NoteType, topic: str = "Road signs", card_count: int = 40, include_images: bool = False
) -> GenerationRequest:
    return GenerationRequest(
        topic=topic,
        language="ru",
        card_count=card_count,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=note_types,
        include_images=include_images,
        instructions=None,
    )


def test_every_generatable_note_type_can_be_requested_together_with_images() -> None:
    note_types = tuple(
        note_type for note_type in NOTE_FIELDS_BY_TYPE if note_type not in UNGENERATABLE_NOTE_TYPES
    )

    assert request_with(*note_types, include_images=True).note_types == note_types


@pytest.mark.parametrize(
    "note_types",
    [(NoteType.IMAGE_OCCLUSION,), (NoteType.BASIC, NoteType.IMAGE_OCCLUSION)],
    ids=["alone", "among other types"],
)
def test_image_occlusion_without_images_is_rejected(note_types: tuple[NoteType, ...]) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="includeImages"):
        request_with(*note_types, include_images=False)


@pytest.mark.parametrize(
    "note_types",
    [(NoteType.IMAGE_OCCLUSION,), (NoteType.BASIC, NoteType.IMAGE_OCCLUSION)],
    ids=["alone", "among other types"],
)
def test_image_occlusion_with_images_is_rejected_as_not_generated_yet(
    note_types: tuple[NoteType, ...],
) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="not generated yet"):
        request_with(*note_types, include_images=True)


def test_every_other_note_type_can_be_requested_without_images() -> None:
    note_types = tuple(
        note_type for note_type in NOTE_FIELDS_BY_TYPE if note_type is not NoteType.IMAGE_OCCLUSION
    )

    assert request_with(*note_types, include_images=False).note_types == note_types


def test_no_note_types_is_rejected() -> None:
    with pytest.raises(InvalidGenerationRequestError):
        request_with()


def test_duplicate_note_types_are_rejected() -> None:
    with pytest.raises(InvalidGenerationRequestError):
        request_with(NoteType.BASIC, NoteType.CLOZE, NoteType.BASIC)


def test_note_type_without_a_field_shape_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    unregister(monkeypatch, NoteType.CLOZE)

    with pytest.raises(InvalidGenerationRequestError, match="cloze"):
        request_with(NoteType.BASIC, NoteType.CLOZE)


def test_basic_optional_reversed_can_be_requested() -> None:
    assert request_with(NoteType.BASIC_OPTIONAL_REVERSED).note_types == (NoteType.BASIC_OPTIONAL_REVERSED,)


BLANK_TOPICS = {
    "empty": "",
    "spaces": "   ",
    "mixed whitespace": " \t\n\r\N{IDEOGRAPHIC SPACE}\N{NO-BREAK SPACE} ",
    "invisible characters": "\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NO-BREAK SPACE}\N{ZERO WIDTH JOINER}",
    "characters that render blank": "\N{HANGUL FILLER} \N{BRAILLE PATTERN BLANK}",
}


@pytest.mark.parametrize("topic", BLANK_TOPICS.values(), ids=BLANK_TOPICS.keys())
def test_blank_topic_is_rejected(topic: str) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="topic"):
        request_with(NoteType.BASIC, topic=topic)


@pytest.mark.parametrize(
    "card_count",
    [MIN_CARD_COUNT - 1, 1, 0, -1, MAX_CARD_COUNT + 1, 10**9],
    ids=repr,
)
def test_card_count_outside_the_bounds_is_rejected(card_count: int) -> None:
    with pytest.raises(InvalidGenerationRequestError, match=f"card count must be .*got {card_count}$"):
        request_with(NoteType.BASIC, card_count=card_count)


@pytest.mark.parametrize(
    "card_count",
    [MIN_CARD_COUNT, MIN_CARD_COUNT + 1, 40, MAX_CARD_COUNT - 1, MAX_CARD_COUNT],
    ids=repr,
)
def test_card_count_within_the_bounds_is_accepted(card_count: int) -> None:
    assert request_with(NoteType.BASIC, card_count=card_count).card_count == card_count


@pytest.mark.parametrize("card_count", [MIN_CARD_COUNT - 1, MAX_CARD_COUNT + 1], ids=repr)
def test_replacing_the_card_count_with_an_out_of_bounds_value_is_rejected(card_count: int) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="card count"):
        replace(request_with(NoteType.BASIC), card_count=card_count)


def test_source_query_carries_the_topic_and_language_exactly_as_requested() -> None:
    request = replace(request_with(NoteType.CLOZE, NoteType.BASIC), topic="  Road   signs ", language="RU")

    assert request.source_query() == SourceQuery(topic="  Road   signs ", language="RU")


def test_topic_with_one_visible_character_among_blanks_is_accepted() -> None:
    topic = " \N{ZERO WIDTH SPACE}x\t"

    assert request_with(NoteType.BASIC, topic=topic).topic == topic


def fingerprint_of(**changes: Unpack[RequestChanges]) -> Fingerprint:
    return replace(request_with(NoteType.BASIC, NoteType.CLOZE), **changes).fingerprint()


def test_note_type_order_does_not_change_the_fingerprint() -> None:
    forward = request_with(NoteType.BASIC, NoteType.CLOZE, NoteType.MULTIPLE_CHOICE).fingerprint()
    backward = request_with(NoteType.MULTIPLE_CHOICE, NoteType.CLOZE, NoteType.BASIC).fingerprint()

    assert forward == backward


@pytest.mark.parametrize(
    "topic",
    [
        "  Road signs",
        "Road signs\n",
        "Road   signs",
        "Road\tsigns",
        "Road\N{NO-BREAK SPACE}signs",
        "Road\N{EM SPACE} signs",
    ],
    ids=["leading", "newline", "repeated", "tab", "no-break space", "em space"],
)
def test_incidental_whitespace_in_the_topic_does_not_change_the_fingerprint(topic: str) -> None:
    assert fingerprint_of(topic=topic) == fingerprint_of(topic="Road signs")


def test_a_decomposed_topic_has_the_fingerprint_of_its_composed_form() -> None:
    assert fingerprint_of(topic="Café vocabulary") == fingerprint_of(topic="Café vocabulary")


def test_topic_casing_is_kept_because_it_can_change_the_meaning() -> None:
    assert fingerprint_of(topic="US states") != fingerprint_of(topic="us states")


@pytest.mark.parametrize(
    ("language", "same_as"), [("RU", "ru"), ("en-GB", "en-gb"), ("zh-Hant-TW", "zh-hant-tw")]
)
def test_language_casing_does_not_change_the_fingerprint(language: str, same_as: str) -> None:
    assert fingerprint_of(language=language) == fingerprint_of(language=same_as)


@pytest.mark.parametrize(
    "changes",
    [
        {"topic": "Road rules"},
        {"language": "en"},
        {"card_count": 41},
        {"difficulty": Difficulty.ADVANCED},
        {"note_types": (NoteType.BASIC,)},
        {"include_images": True},
        {"instructions": "Only European signs"},
    ],
    ids=["topic", "language", "card_count", "difficulty", "note_types", "include_images", "instructions"],
)
def test_every_field_that_shapes_the_output_changes_the_fingerprint(changes: RequestChanges) -> None:
    assert fingerprint_of(**changes) != fingerprint_of()


@pytest.mark.parametrize("instructions", [None, "", "   ", "\n\t", "\u200b"], ids=repr)
def test_absent_and_blank_instructions_share_a_fingerprint(instructions: str | None) -> None:
    assert fingerprint_of(instructions=instructions) == fingerprint_of(instructions=None)


def test_instruction_whitespace_does_not_change_the_fingerprint() -> None:
    assert fingerprint_of(instructions="  Only   European\nsigns ") == fingerprint_of(
        instructions="Only European signs"
    )


def test_fingerprint_leaves_the_request_itself_untouched() -> None:
    request = replace(request_with(NoteType.CLOZE, NoteType.BASIC), topic="  Road   signs ", language="RU")

    request.fingerprint()

    assert request.topic == "  Road   signs "
    assert request.language == "RU"
    assert request.note_types == (NoteType.CLOZE, NoteType.BASIC)


DOCUMENT_ID = UUID("5d0c7a1e-3b2f-4c8d-9e6f-1a2b3c4d5e6f")
OTHER_DOCUMENT_ID = UUID("6e1d8b2f-4c3a-4d9e-8f7a-2b3c4d5e6f70")


RECORDINGS = sorted(RECORDING_FORMATS)
TEXT_DOCUMENTS = sorted(set(DocumentFormat) - RECORDING_FORMATS)


def document_request_with(**changes: Unpack[RequestChanges]) -> GenerationRequest:
    request = replace(request_with(NoteType.BASIC, NoteType.CLOZE), topic=None, document_id=DOCUMENT_ID)
    return replace(request, **changes)


def stored(document_format: DocumentFormat = DocumentFormat.PDF, document_id: UUID = DOCUMENT_ID) -> Document:
    return Document(
        document_id=document_id,
        client_id=UUID(int=1, version=4),
        filename="lecture",
        size_bytes=1024,
        document_format=document_format,
        uploaded_at=datetime(2026, 10, 18, tzinfo=UTC),
        duration_seconds=60.0 if document_format in RECORDING_FORMATS else None,
    )


def test_request_from_a_document_names_no_topic_and_is_a_document_source() -> None:
    request = document_request_with()

    assert request.topic is None
    assert request.document_id == DOCUMENT_ID
    assert request.source_kind(stored()) is SourceKind.DOCUMENT
    assert request_with(NoteType.BASIC).source_kind(None) is SourceKind.TOPIC


@pytest.mark.parametrize("document_format", RECORDINGS)
def test_request_naming_a_recording_is_a_recording_source(document_format: DocumentFormat) -> None:
    assert document_request_with().source_kind(stored(document_format)) is SourceKind.RECORDING


@pytest.mark.parametrize("document_format", TEXT_DOCUMENTS)
def test_request_naming_a_text_document_stays_a_document_source(document_format: DocumentFormat) -> None:
    assert document_request_with().source_kind(stored(document_format)) is SourceKind.DOCUMENT


def test_request_naming_a_document_that_could_not_be_found_is_counted_as_a_document_source() -> None:
    assert document_request_with().source_kind(None) is SourceKind.DOCUMENT


@pytest.mark.parametrize(
    "request_kind",
    ["topic", "another document"],
)
def test_kind_and_fingerprint_refuse_a_document_the_request_does_not_name(request_kind: str) -> None:
    request = request_with(NoteType.BASIC) if request_kind == "topic" else document_request_with()
    unnamed = stored(DocumentFormat.M4A, OTHER_DOCUMENT_ID)

    with pytest.raises(InvalidGenerationRequestError, match="not the source this request names"):
        request.source_kind(unnamed)
    with pytest.raises(InvalidGenerationRequestError, match="not the source this request names"):
        request.fingerprint(unnamed)


def test_request_from_a_document_cannot_be_fingerprinted_without_its_document() -> None:
    with pytest.raises(InvalidGenerationRequestError, match="fingerprinted together with that document"):
        document_request_with().fingerprint()


@pytest.mark.parametrize("document_format", RECORDINGS)
def test_recording_fingerprint_names_the_recording_and_normalises_like_a_document_fingerprint(
    document_format: DocumentFormat,
) -> None:
    request = document_request_with(language="RU", note_types=(NoteType.CLOZE, NoteType.BASIC))

    assert request.fingerprint(stored(document_format)) == RecordingGenerationFingerprint(
        document_id=DOCUMENT_ID,
        language="ru",
        card_count=40,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=(NoteType.BASIC, NoteType.CLOZE),
        include_images=False,
        instructions=None,
    )


def test_recording_and_document_fingerprints_of_the_same_settings_are_never_equal() -> None:
    request = document_request_with()

    recording = request.fingerprint(stored(DocumentFormat.M4A))
    document = request.fingerprint(stored(DocumentFormat.PDF))

    assert recording != document
    assert asdict(recording) == asdict(document)


@pytest.mark.parametrize(
    ("topic", "document_id"),
    [("Road signs", DOCUMENT_ID), (None, None)],
    ids=["both", "neither"],
)
def test_request_must_name_exactly_one_of_a_topic_and_a_document(
    topic: str | None, document_id: UUID | None
) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="exactly one"):
        replace(request_with(NoteType.BASIC), topic=topic, document_id=document_id)


@pytest.mark.parametrize("document_id", [UUID(int=0), UUID(int=7, version=1)], ids=["nil", "version 1"])
def test_document_id_must_be_a_version_4_uuid(document_id: UUID) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="documentId"):
        document_request_with(document_id=document_id)


def test_request_from_a_document_still_validates_the_rest_of_the_request() -> None:
    with pytest.raises(InvalidGenerationRequestError, match="card count"):
        document_request_with(card_count=MAX_CARD_COUNT + 1)


def test_request_from_a_document_has_no_topic_to_search_for() -> None:
    with pytest.raises(InvalidGenerationRequestError, match="no topic"):
        document_request_with().source_query()


def test_document_fingerprint_names_the_document_and_normalises_like_a_topic_fingerprint() -> None:
    request = document_request_with(language="RU", note_types=(NoteType.CLOZE, NoteType.BASIC))

    assert request.fingerprint(stored()) == DocumentGenerationFingerprint(
        document_id=DOCUMENT_ID,
        language="ru",
        card_count=40,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=(NoteType.BASIC, NoteType.CLOZE),
        include_images=False,
        instructions=None,
    )
    assert document_request_with(instructions=" \t ").fingerprint(
        stored()
    ) == document_request_with().fingerprint(stored())


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": OTHER_DOCUMENT_ID},
        {"language": "en"},
        {"card_count": 41},
        {"difficulty": Difficulty.ADVANCED},
        {"note_types": (NoteType.BASIC,)},
        {"include_images": True},
        {"instructions": "Only chapter two"},
    ],
    ids=[
        "document_id",
        "language",
        "card_count",
        "difficulty",
        "note_types",
        "include_images",
        "instructions",
    ],
)
def test_every_input_that_shapes_the_deck_changes_the_document_fingerprint(
    changes: RequestChanges,
) -> None:
    changed = document_request_with(**changes)
    assert changed.fingerprint(stored(document_id=changed.document_id or DOCUMENT_ID)) != (
        document_request_with().fingerprint(stored())
    )


def test_document_and_topic_fingerprints_of_otherwise_equal_requests_differ() -> None:
    topic = request_with(NoteType.BASIC, NoteType.CLOZE)

    assert document_request_with().fingerprint(stored()) != topic.fingerprint()
    assert isinstance(topic.fingerprint(), GenerationFingerprint)
