import pytest

from deckly.domain.exceptions import InvalidGenerationRequestError
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE
from tests.domain.builders import unregister


def request_with(
    *note_types: NoteType, topic: str = "Road signs", include_images: bool = False
) -> GenerationRequest:
    return GenerationRequest(
        topic=topic,
        language="ru",
        card_count=40,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=note_types,
        include_images=include_images,
        instructions=None,
    )


def test_every_registered_note_type_can_be_requested_together_with_images() -> None:
    note_types = tuple(NOTE_FIELDS_BY_TYPE)

    assert request_with(*note_types, include_images=True).note_types == note_types


@pytest.mark.parametrize(
    "note_types",
    [(NoteType.IMAGE_OCCLUSION,), (NoteType.BASIC, NoteType.IMAGE_OCCLUSION)],
    ids=["alone", "among other types"],
)
def test_image_occlusion_without_images_is_rejected(note_types: tuple[NoteType, ...]) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="includeImages"):
        request_with(*note_types, include_images=False)


def test_image_occlusion_with_images_is_accepted() -> None:
    request = request_with(NoteType.IMAGE_OCCLUSION, include_images=True)

    assert request.note_types == (NoteType.IMAGE_OCCLUSION,)


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


def test_topic_with_one_visible_character_among_blanks_is_accepted() -> None:
    topic = " \N{ZERO WIDTH SPACE}x\t"

    assert request_with(NoteType.BASIC, topic=topic).topic == topic
