import pytest

from deckly.domain.exceptions import InvalidGenerationRequestError
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE


def request_with(*note_types: NoteType) -> GenerationRequest:
    return GenerationRequest(
        topic="Road signs",
        language="ru",
        card_count=40,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=note_types,
        include_images=False,
        instructions=None,
    )


def test_every_registered_note_type_can_be_requested_together() -> None:
    note_types = tuple(NOTE_FIELDS_BY_TYPE)

    assert request_with(*note_types).note_types == note_types


def test_no_note_types_is_rejected() -> None:
    with pytest.raises(InvalidGenerationRequestError):
        request_with()


def test_duplicate_note_types_are_rejected() -> None:
    with pytest.raises(InvalidGenerationRequestError):
        request_with(NoteType.BASIC, NoteType.CLOZE, NoteType.BASIC)


@pytest.mark.parametrize("unregistered", sorted(set(NoteType) - set(NOTE_FIELDS_BY_TYPE)))
def test_note_type_without_a_field_shape_is_rejected(unregistered: NoteType) -> None:
    with pytest.raises(InvalidGenerationRequestError, match=unregistered):
        request_with(NoteType.BASIC, unregistered)


def test_basic_optional_reversed_is_still_unregistered() -> None:
    assert NoteType.BASIC_OPTIONAL_REVERSED not in NOTE_FIELDS_BY_TYPE
