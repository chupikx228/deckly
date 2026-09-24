import math
import time
from dataclasses import replace
from uuid import UUID

import pytest

from deckly.domain.exceptions import (
    DistractorMatchesAnswerError,
    InvalidClozeError,
    InvalidNoteError,
    UnsupportedNoteTypeError,
)
from deckly.domain.notes.basic import BasicFields, BasicReversedFields, BasicTypeInFields, FrontBackFields
from deckly.domain.notes.cloze import ClozeFields
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.image_occlusion import ImageOcclusionFields, OcclusionRegion
from deckly.domain.notes.multiple_choice import MultipleChoiceFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE, fields_type_for
from tests.domain.builders import client_id

FRONT_BACK_TYPES: list[type[FrontBackFields]] = [BasicFields, BasicReversedFields, BasicTypeInFields]
BLANKS = ["", " ", "\n\t", "\N{NO-BREAK SPACE}", "\N{ZERO WIDTH NO-BREAK SPACE}"]


VALID_REGION = OcclusionRegion(ordinal=1, x=0.1, y=0.1, width=0.2, height=0.2)


def region(ordinal: int = 1, **coordinates: float) -> OcclusionRegion:
    return replace(VALID_REGION, ordinal=ordinal, **coordinates)


def multiple_choice(answer: str = "Paris", *distractors: str) -> MultipleChoiceFields:
    return MultipleChoiceFields(
        question="Capital of France?", answer=answer, distractors=distractors or ("Lyon", "Nice")
    )


@pytest.mark.parametrize("fields_type", FRONT_BACK_TYPES)
def test_front_back_types_accept_front_and_back(fields_type: type[FrontBackFields]) -> None:
    note = Note(client_id=client_id(1), fields=fields_type(front="Red triangle", back="Warning"))

    assert note.note_type is fields_type.note_type


@pytest.mark.parametrize("fields_type", FRONT_BACK_TYPES)
@pytest.mark.parametrize("blank", BLANKS)
def test_front_back_types_reject_a_blank_side(fields_type: type[FrontBackFields], blank: str) -> None:
    with pytest.raises(InvalidNoteError):
        fields_type(front=blank, back="Warning")
    with pytest.raises(InvalidNoteError):
        fields_type(front="Red triangle", back=blank)


@pytest.mark.parametrize(
    "text",
    [
        "{{c1::Paris}} is the capital of France",
        "{{c1::Paris}} is the capital of {{c2::France}}",
        "{{c2::Paris}} is the capital of {{c1::France}}",
        "{{c1::Paris}} and {{c1::Lyon}} are in France",
        "{{c1::Paris::city}} is a capital",
        "Line one\n{{c1::Paris}}\n{{c2::France}}",
    ],
)
def test_cloze_accepts_markers_numbered_from_one_without_gaps(text: str) -> None:
    assert ClozeFields(text=text).text == text


@pytest.mark.parametrize(
    "text",
    [
        "Paris is the capital of France",
        "{{c2::Paris}} is the capital",
        "{{c1::Paris}} is the capital of {{c3::France}}",
        "{{c0::Paris}} is the capital",
        "{{c0::Paris}} and {{c1::France}}",
        "{{c01::Paris}} is the capital",
        "{{c1::}} is the capital",
        "{{c1::   }} is the capital",
        "{{c1::::hint}} is the capital",
        "{{c1::Paris is the capital",
        "{{c1::Paris}} and {{c2::France",
        "{{c1::Paris {{c2::France}} }}",
        "{{c1::Par\nis}} is the capital",
        "{{C1::Paris}} is the capital",
        "{{c\N{ARABIC-INDIC DIGIT ONE}::Paris}} is the capital",
        "{{c1:Paris}} is the capital",
        "{{c1::Paris}} and {{c2::Fr\nance}}",
        "{{c1::Par\ris}} is the capital",
        "{{c1::Par\N{LINE SEPARATOR}is}} is the capital",
        "{{c1::Paris}} and {{c" + "1" * 5000 + "::France}}",
    ],
    ids=[
        "no-marker",
        "starts-at-two",
        "gap",
        "zero",
        "zero-and-one",
        "leading-zero",
        "empty-answer",
        "blank-answer",
        "blank-answer-with-hint",
        "unterminated",
        "second-unterminated",
        "nested",
        "multi-line-answer",
        "uppercase",
        "non-ascii-digit",
        "single-colon",
        "multi-line-after-a-valid-marker",
        "carriage-return",
        "line-separator",
        "number-too-long-to-parse",
    ],
)
def test_cloze_rejects_missing_malformed_or_gapped_markers(text: str) -> None:
    with pytest.raises(InvalidClozeError):
        ClozeFields(text=text)


def test_cloze_rejects_many_unterminated_markers_in_linear_time() -> None:
    started = time.perf_counter()

    with pytest.raises(InvalidClozeError):
        ClozeFields(text="{{c1::Paris}}" + "{{c1::" * 50_000)

    assert time.perf_counter() - started < 1.0


def test_cloze_rejects_blank_text() -> None:
    with pytest.raises(InvalidNoteError):
        ClozeFields(text="  ")


def test_cloze_extra_is_optional() -> None:
    assert ClozeFields(text="{{c1::Paris}}").extra == ""


@pytest.mark.parametrize(
    "distractors", [("Lyon", "Nice"), ("Lyon", "Nice", "Lille", "Metz")], ids=["two", "four"]
)
def test_multiple_choice_accepts_two_to_four_distractors(distractors: tuple[str, ...]) -> None:
    assert multiple_choice("Paris", *distractors).distractors == distractors


@pytest.mark.parametrize(
    "distractors",
    [(), ("Lyon",), ("Lyon", "Nice", "Lille", "Metz", "Brest")],
    ids=["zero", "one", "five"],
)
def test_multiple_choice_rejects_distractor_count_outside_two_to_four(distractors: tuple[str, ...]) -> None:
    with pytest.raises(InvalidNoteError):
        MultipleChoiceFields(question="Capital of France?", answer="Paris", distractors=distractors)


@pytest.mark.parametrize(
    "matching", ["Paris", "paris", "  PARIS ", "\N{FULLWIDTH LATIN CAPITAL LETTER P}aris"]
)
def test_multiple_choice_rejects_a_distractor_matching_the_answer(matching: str) -> None:
    with pytest.raises(DistractorMatchesAnswerError):
        multiple_choice("Paris", "Lyon", matching)


def test_multiple_choice_rejects_an_answer_matching_a_distractor_up_to_whitespace() -> None:
    with pytest.raises(DistractorMatchesAnswerError):
        multiple_choice("New  York", "New York", "Boston")


def test_distractor_matching_answer_is_an_invalid_note() -> None:
    assert issubclass(DistractorMatchesAnswerError, InvalidNoteError)


@pytest.mark.parametrize("duplicates", [("Lyon", "Lyon"), ("Lyon", "lyon "), ("Lyon", "Nice", "LYON")])
def test_multiple_choice_rejects_duplicate_distractors(duplicates: tuple[str, ...]) -> None:
    with pytest.raises(InvalidNoteError):
        multiple_choice("Paris", *duplicates)


@pytest.mark.parametrize("blank", BLANKS)
def test_multiple_choice_rejects_blank_text(blank: str) -> None:
    with pytest.raises(InvalidNoteError):
        multiple_choice(blank)
    with pytest.raises(InvalidNoteError):
        multiple_choice("Paris", "Lyon", blank)
    with pytest.raises(InvalidNoteError):
        MultipleChoiceFields(question=blank, answer="Paris", distractors=("Lyon", "Nice"))


def test_image_occlusion_accepts_normalised_regions() -> None:
    fields = ImageOcclusionFields(image_id="img-1", regions=(region(1), region(2, x=0.5)))

    assert len(fields.regions) == 2
    assert fields.extra == ""


@pytest.mark.parametrize("value", [0.0, 1.0])
@pytest.mark.parametrize("coordinate", ["x", "y", "width", "height"])
def test_region_accepts_the_bounds_of_the_unit_interval(coordinate: str, value: float) -> None:
    region(1, **{coordinate: value})


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("coordinate", ["x", "y", "width", "height"])
def test_region_rejects_coordinates_outside_the_unit_interval(coordinate: str, value: float) -> None:
    with pytest.raises(InvalidNoteError):
        region(1, **{coordinate: value})


@pytest.mark.parametrize("ordinal", [0, -1])
def test_region_rejects_an_ordinal_below_one(ordinal: int) -> None:
    with pytest.raises(InvalidNoteError):
        region(ordinal)


def test_image_occlusion_rejects_no_regions() -> None:
    with pytest.raises(InvalidNoteError):
        ImageOcclusionFields(image_id="img-1", regions=())


@pytest.mark.parametrize("blank", BLANKS)
def test_image_occlusion_rejects_a_blank_image_id(blank: str) -> None:
    with pytest.raises(InvalidNoteError):
        ImageOcclusionFields(image_id=blank, regions=(region(),))


def test_note_rejects_a_client_id_that_is_not_uuid_v4() -> None:
    with pytest.raises(InvalidNoteError):
        Note(client_id=UUID(int=1), fields=BasicFields(front="a", back="b"))


@pytest.mark.parametrize(
    "fields", [FrontBackFields(front="a", back="b"), NoteFields()], ids=["front-back", "base"]
)
def test_note_rejects_a_field_shape_that_is_not_registered(fields: NoteFields) -> None:
    with pytest.raises(UnsupportedNoteTypeError):
        Note(client_id=client_id(1), fields=fields)


def test_every_registered_shape_declares_the_type_it_is_registered_under() -> None:
    assert all(fields_type.note_type is note_type for note_type, fields_type in NOTE_FIELDS_BY_TYPE.items())


@pytest.mark.parametrize(
    "note_type",
    [
        NoteType.BASIC,
        NoteType.BASIC_REVERSED,
        NoteType.BASIC_TYPE_IN,
        NoteType.CLOZE,
        NoteType.MULTIPLE_CHOICE,
        NoteType.IMAGE_OCCLUSION,
    ],
)
def test_every_contract_documented_note_type_has_a_field_shape(note_type: NoteType) -> None:
    assert fields_type_for(note_type).note_type is note_type


def test_a_note_type_without_a_documented_shape_is_unsupported() -> None:
    with pytest.raises(UnsupportedNoteTypeError):
        fields_type_for(NoteType.BASIC_OPTIONAL_REVERSED)
