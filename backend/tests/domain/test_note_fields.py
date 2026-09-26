import math
import time
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from deckly.domain.exceptions import (
    DistractorMatchesAnswerError,
    InvalidClozeError,
    InvalidNoteError,
    MissingSourceError,
    UnsupportedNoteTypeError,
)
from deckly.domain.media import Media, MediaKind
from deckly.domain.notes.basic import (
    BasicFields,
    BasicOptionalReversedFields,
    BasicReversedFields,
    BasicTypeInFields,
    FrontBackFields,
)
from deckly.domain.notes.cloze import ClozeFields
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.image_occlusion import ImageOcclusionFields, OcclusionRegion
from deckly.domain.notes.multiple_choice import MultipleChoiceFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE, fields_type_for
from tests.domain.builders import SOURCES, client_id, unregister

FRONT_BACK_TYPES: list[type[FrontBackFields]] = [BasicFields, BasicReversedFields, BasicTypeInFields]
BLANKS = [
    "",
    " ",
    "\n\t",
    "\N{NO-BREAK SPACE}",
    "\N{ZERO WIDTH NO-BREAK SPACE}",
    "\N{ZERO WIDTH SPACE}",
    "\N{ZERO WIDTH JOINER} \N{RIGHT-TO-LEFT MARK}",
    "\x07\x1b",
    "\N{COMBINING ACUTE ACCENT}",
    "\N{HANGUL FILLER}",
    "\N{BRAILLE PATTERN BLANK}",
]


VALID_REGION = OcclusionRegion(ordinal=1, x=0.1, y=0.1, width=0.2, height=0.2)
IMAGE = Media(
    media_id=UUID("3fa85f64-5717-4562-b3fc-2c963f66afa6"),
    kind=MediaKind.IMAGE,
    url="https://cdn.example.com/heart.png",
    license="CC-BY-4.0",
    alt="Heart diagram",
)
IMAGE_ID = str(IMAGE.media_id)


def region(ordinal: int = 1, **coordinates: float) -> OcclusionRegion:
    return replace(VALID_REGION, ordinal=ordinal, **coordinates)


def occlusion_note(image_id: str = IMAGE_ID, *media: Media) -> Note:
    fields = ImageOcclusionFields(image_id=image_id, regions=(region(),))
    return Note(client_id=client_id(1), fields=fields, sources=SOURCES, media=media)


def multiple_choice(answer: str = "Paris", *distractors: str) -> MultipleChoiceFields:
    return MultipleChoiceFields(
        question="Capital of France?", answer=answer, distractors=distractors or ("Lyon", "Nice")
    )


@pytest.mark.parametrize("fields_type", FRONT_BACK_TYPES)
def test_front_back_types_accept_front_and_back(fields_type: type[FrontBackFields]) -> None:
    note = Note(
        client_id=client_id(1), fields=fields_type(front="Red triangle", back="Warning"), sources=SOURCES
    )

    assert note.note_type is fields_type.note_type


@pytest.mark.parametrize("fields_type", FRONT_BACK_TYPES)
@pytest.mark.parametrize("blank", BLANKS)
def test_front_back_types_reject_a_blank_side(fields_type: type[FrontBackFields], blank: str) -> None:
    with pytest.raises(InvalidNoteError):
        fields_type(front=blank, back="Warning")
    with pytest.raises(InvalidNoteError):
        fields_type(front="Red triangle", back=blank)


@pytest.mark.parametrize("add_reverse", [True, False])
def test_optional_reversed_accepts_front_back_and_the_reverse_flag(*, add_reverse: bool) -> None:
    fields = BasicOptionalReversedFields(front="Red triangle", back="Warning", add_reverse=add_reverse)

    note = Note(client_id=client_id(1), fields=fields, sources=SOURCES)

    assert note.note_type is NoteType.BASIC_OPTIONAL_REVERSED
    assert fields.add_reverse is add_reverse


@pytest.mark.parametrize("blank", BLANKS)
def test_optional_reversed_rejects_a_blank_side(blank: str) -> None:
    with pytest.raises(InvalidNoteError):
        BasicOptionalReversedFields(front=blank, back="Warning", add_reverse=True)
    with pytest.raises(InvalidNoteError):
        BasicOptionalReversedFields(front="Red triangle", back=blank, add_reverse=True)


@pytest.mark.parametrize("flag", ["false", "true", "", 0, 1, None], ids=repr)
def test_optional_reversed_rejects_a_reverse_flag_that_is_not_a_boolean(flag: object) -> None:
    with pytest.raises(InvalidNoteError, match="addReverse"):
        BasicOptionalReversedFields(front="Red triangle", back="Warning", add_reverse=cast("bool", flag))


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
        "{{c1::\N{HANGUL FILLER}}} is the capital",
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
        "blank-looking-answer",
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


STILL_FOLDED: dict[str, tuple[str, str]] = {
    "case even where it changes the meaning": ("Polish", "polish"),
    "capital and small sharp s": (
        "STRA\N{LATIN CAPITAL LETTER SHARP S}E",
        "stra\N{LATIN SMALL LETTER SHARP S}e",
    ),
    "halfwidth katakana with a voiced sound mark": (
        "\N{KATAKANA LETTER GA}",
        "\N{HALFWIDTH KATAKANA LETTER KA}\N{HALFWIDTH KATAKANA VOICED SOUND MARK}",
    ),
    "ideographic space between words": ("New York", "New\N{IDEOGRAPHIC SPACE}York"),
    "greek word ending in a final sigma": ("ΟΔΟΣ", "οδος"),
    "iota subscript written apart from its capital": (
        "\N{GREEK CAPITAL LETTER ALPHA}\N{COMBINING GREEK YPOGEGRAMMENI}",
        "\N{GREEK SMALL LETTER ALPHA WITH YPOGEGRAMMENI}",
    ),
    "accent that only composes once the letter is folded": (
        "J\N{COMBINING CARON}",
        "\N{LATIN SMALL LETTER J WITH CARON}",
    ),
}


@pytest.mark.parametrize(("answer", "distractor"), STILL_FOLDED.values(), ids=STILL_FOLDED.keys())
def test_multiple_choice_still_folds_case_width_and_spacing(answer: str, distractor: str) -> None:
    with pytest.raises(DistractorMatchesAnswerError):
        multiple_choice(answer, "Boston", distractor)


COMPATIBILITY_FORMS: dict[str, tuple[str, str]] = {
    "superscript and subscript digit": ("x\N{SUPERSCRIPT TWO}", "x\N{SUBSCRIPT TWO}"),
    "superscript and plain digit": ("x\N{SUPERSCRIPT TWO}", "x2"),
    "sharp s and double s": ("Stra\N{LATIN SMALL LETTER SHARP S}e", "Strasse"),
    "ligature and its letters": ("\N{LATIN SMALL LIGATURE FI}sh", "fish"),
    "vulgar fraction and its digits": ("\N{VULGAR FRACTION ONE HALF}", "1\N{FRACTION SLASH}2"),
    "roman numeral and its letters": ("\N{ROMAN NUMERAL FOUR}", "IV"),
}


@pytest.mark.parametrize(("first", "second"), COMPATIBILITY_FORMS.values(), ids=COMPATIBILITY_FORMS.keys())
def test_multiple_choice_keeps_compatibility_forms_apart(first: str, second: str) -> None:
    assert multiple_choice(first, "Boston", second).distractors == ("Boston", second)
    assert multiple_choice(second, "Boston", first).distractors == ("Boston", first)
    assert multiple_choice("Boston", first, second).distractors == (first, second)


INVISIBLY_PADDED_ANSWERS = {
    "trailing zero-width space": "Paris\N{ZERO WIDTH SPACE}",
    "leading byte order mark": "\N{ZERO WIDTH NO-BREAK SPACE}Paris",
    "inner soft hyphen": "Pa\N{SOFT HYPHEN}ris",
    "inner zero-width joiner": "Pa\N{ZERO WIDTH JOINER}ris",
    "trailing right-to-left mark": "Paris\N{RIGHT-TO-LEFT MARK}",
    "trailing variation selector": "Paris\N{VARIATION SELECTOR-16}",
    "trailing combining grapheme joiner": "Paris\N{COMBINING GRAPHEME JOINER}",
    "trailing hangul filler": "Paris\N{HANGUL FILLER}",
    "trailing braille blank": "Paris\N{BRAILLE PATTERN BLANK}",
    "trailing tag character": "Paris\U000e0002",
    "trailing control character": "Paris\x07",
}


@pytest.mark.parametrize("padded", INVISIBLY_PADDED_ANSWERS.values(), ids=INVISIBLY_PADDED_ANSWERS.keys())
def test_multiple_choice_rejects_a_distractor_matching_the_answer_up_to_invisible_characters(
    padded: str,
) -> None:
    with pytest.raises(DistractorMatchesAnswerError):
        multiple_choice("Paris", "Lyon", padded)
    with pytest.raises(DistractorMatchesAnswerError):
        multiple_choice(padded, "Lyon", "Paris")


@pytest.mark.parametrize("padded", INVISIBLY_PADDED_ANSWERS.values(), ids=INVISIBLY_PADDED_ANSWERS.keys())
def test_multiple_choice_rejects_distractors_duplicated_up_to_invisible_characters(padded: str) -> None:
    with pytest.raises(InvalidNoteError, match="mutually exclusive"):
        multiple_choice("Lyon", "Paris", padded)


def test_invisible_character_between_words_does_not_stand_in_for_a_space() -> None:
    fields = multiple_choice("New York", "New\N{ZERO WIDTH SPACE}York", "Boston")

    assert fields.answer == "New York"


def test_multiple_choice_treats_combining_marks_as_significant() -> None:
    fields = multiple_choice("за\N{COMBINING ACUTE ACCENT}мок", "замо\N{COMBINING ACUTE ACCENT}к", "замок")

    assert len(fields.distractors) == 2


def test_multiple_choice_keeps_subdivision_flags_distinct() -> None:
    scotland = "\N{WAVING BLACK FLAG}\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
    england = "\N{WAVING BLACK FLAG}\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
    wales = "\N{WAVING BLACK FLAG}\U000e0067\U000e0062\U000e0077\U000e006c\U000e0073\U000e007f"

    fields = multiple_choice(scotland, england, wales)

    assert fields.distractors == (england, wales)


def test_multiple_choice_strips_invisible_characters_before_composing_accents() -> None:
    with pytest.raises(DistractorMatchesAnswerError):
        multiple_choice(
            "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
            "Lyon",
            "cafe\N{ZERO WIDTH SPACE}\N{COMBINING ACUTE ACCENT}",
        )


@pytest.mark.parametrize("separator", ["\t", "\n", "\r\n"], ids=["tab", "newline", "crlf"])
def test_multiple_choice_still_treats_control_whitespace_as_a_space(separator: str) -> None:
    with pytest.raises(DistractorMatchesAnswerError):
        multiple_choice("New York", "Boston", f"New{separator}York")


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
    fields = ImageOcclusionFields(image_id=IMAGE_ID, regions=(region(1), region(2, x=0.5)))

    assert len(fields.regions) == 2
    assert fields.extra == ""


@pytest.mark.parametrize(
    "coordinates",
    [
        {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        {"x": 0.8, "y": 0.8, "width": 0.2, "height": 0.2},
        {"x": 0.1, "y": 0.7, "width": 0.9, "height": 0.3},
        {"x": 0.35, "y": 0.65, "width": 0.65, "height": 0.35},
    ],
    ids=["whole image", "touching the far edges", "tenths summing to one", "hundredths summing to one"],
)
def test_region_may_reach_the_edges_of_the_image(coordinates: dict[str, float]) -> None:
    assert region(1, **coordinates) == OcclusionRegion(ordinal=1, **coordinates)


@pytest.mark.parametrize(
    "coordinates",
    [
        {"x": 0.9, "width": 0.2},
        {"y": 0.9, "height": 0.2},
        {"x": 0.5, "width": 0.500001},
        {"x": 1.0, "width": 0.2},
        {"x": 0.0, "width": 1.0, "y": 0.5, "height": 0.6},
    ],
    ids=[
        "past the right edge",
        "past the bottom edge",
        "just past the right edge",
        "starting on the edge",
        "tall",
    ],
)
def test_region_must_lie_entirely_within_the_image(coordinates: dict[str, float]) -> None:
    with pytest.raises(InvalidNoteError, match="within the image"):
        region(1, **coordinates)


@pytest.mark.parametrize("dimension", ["width", "height"])
@pytest.mark.parametrize("zero", [0.0, -0.0])
def test_region_must_not_be_degenerate(dimension: str, zero: float) -> None:
    with pytest.raises(InvalidNoteError, match="non-zero"):
        region(1, **{dimension: zero})


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("coordinate", ["x", "y", "width", "height"])
def test_region_rejects_coordinates_outside_the_unit_interval(coordinate: str, value: float) -> None:
    with pytest.raises(InvalidNoteError):
        region(1, **{coordinate: value})


@pytest.mark.parametrize("ordinal", [0, -1])
def test_region_rejects_an_ordinal_below_one(ordinal: int) -> None:
    with pytest.raises(InvalidNoteError):
        region(ordinal)


@pytest.mark.parametrize("ordinals", [(1, 3), (2, 7, 5), (4,)], ids=["gap", "unordered", "single above one"])
def test_region_ordinals_may_skip_numbers(ordinals: tuple[int, ...]) -> None:
    regions = tuple(region(ordinal) for ordinal in ordinals)

    assert ImageOcclusionFields(image_id=IMAGE_ID, regions=regions).regions == regions


@pytest.mark.parametrize("ordinals", [(1, 1), (1, 2, 1), (3, 5, 5)], ids=["pair", "split", "after a gap"])
def test_region_ordinals_must_be_unique(ordinals: tuple[int, ...]) -> None:
    with pytest.raises(InvalidNoteError, match="unique"):
        ImageOcclusionFields(image_id=IMAGE_ID, regions=tuple(region(ordinal) for ordinal in ordinals))


def test_image_occlusion_note_referencing_its_own_image_is_accepted() -> None:
    audio = replace(IMAGE, media_id=client_id(10), kind=MediaKind.AUDIO, alt=None)

    assert occlusion_note(IMAGE_ID, audio, IMAGE).media == (audio, IMAGE)


@pytest.mark.parametrize(
    ("image_id", "media"),
    [
        (IMAGE_ID, ()),
        (str(client_id(10)), (IMAGE,)),
        (IMAGE_ID, (replace(IMAGE, kind=MediaKind.AUDIO, alt=None),)),
        (IMAGE_ID.upper(), (IMAGE,)),
        ("img-1", (IMAGE,)),
    ],
    ids=["no media", "another image's id", "audio with that id", "non-canonical id", "not an id"],
)
def test_image_occlusion_note_must_reference_one_of_its_own_images(
    image_id: str, media: tuple[Media, ...]
) -> None:
    with pytest.raises(InvalidNoteError, match="imageId"):
        occlusion_note(image_id, *media)


def test_image_occlusion_rejects_no_regions() -> None:
    with pytest.raises(InvalidNoteError):
        ImageOcclusionFields(image_id=IMAGE_ID, regions=())


@pytest.mark.parametrize("blank", BLANKS)
def test_image_occlusion_rejects_a_blank_image_id(blank: str) -> None:
    with pytest.raises(InvalidNoteError):
        ImageOcclusionFields(image_id=blank, regions=(region(),))


def test_note_without_a_source_is_rejected() -> None:
    with pytest.raises(MissingSourceError):
        Note(client_id=client_id(1), fields=BasicFields(front="a", back="b"), sources=())


def test_missing_source_is_an_invalid_note_so_only_that_note_is_dropped() -> None:
    assert issubclass(MissingSourceError, InvalidNoteError)


def test_note_rejects_a_client_id_that_is_not_uuid_v4() -> None:
    with pytest.raises(InvalidNoteError):
        Note(client_id=UUID(int=1), fields=BasicFields(front="a", back="b"), sources=SOURCES)


@pytest.mark.parametrize(
    "fields", [FrontBackFields(front="a", back="b"), NoteFields()], ids=["front-back", "base"]
)
def test_note_rejects_a_field_shape_that_is_not_registered(fields: NoteFields) -> None:
    with pytest.raises(UnsupportedNoteTypeError):
        Note(client_id=client_id(1), fields=fields, sources=SOURCES)


def test_every_registered_shape_declares_the_type_it_is_registered_under() -> None:
    assert all(fields_type.note_type is note_type for note_type, fields_type in NOTE_FIELDS_BY_TYPE.items())


@pytest.mark.parametrize(
    "note_type",
    [
        NoteType.BASIC,
        NoteType.BASIC_REVERSED,
        NoteType.BASIC_OPTIONAL_REVERSED,
        NoteType.BASIC_TYPE_IN,
        NoteType.CLOZE,
        NoteType.MULTIPLE_CHOICE,
        NoteType.IMAGE_OCCLUSION,
    ],
)
def test_every_contract_documented_note_type_has_a_field_shape(note_type: NoteType) -> None:
    assert fields_type_for(note_type).note_type is note_type


def test_every_contract_note_type_is_registered() -> None:
    assert set(NOTE_FIELDS_BY_TYPE) == set(NoteType)


def test_a_note_type_without_a_registered_shape_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    unregister(monkeypatch, NoteType.BASIC)

    with pytest.raises(UnsupportedNoteTypeError):
        fields_type_for(NoteType.BASIC)
