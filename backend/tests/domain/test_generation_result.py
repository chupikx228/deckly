import pytest

from deckly.domain.deck import MAX_DESCRIPTION_LENGTH, MAX_TITLE_LENGTH, Deck
from deckly.domain.exceptions import DuplicateClientIdError, InvalidDeckError
from deckly.domain.notes.cloze import ClozeFields
from deckly.domain.notes.note import Note
from tests.domain.builders import basic_note, client_id, result_with


def test_result_with_unique_client_ids_is_accepted() -> None:
    result = result_with(basic_note(1), basic_note(2), basic_note(3))

    assert len(result.notes) == 3


def test_duplicate_client_id_is_rejected() -> None:
    with pytest.raises(DuplicateClientIdError):
        result_with(basic_note(1), basic_note(2), basic_note(1))


def test_duplicate_client_id_is_rejected_even_when_the_notes_differ() -> None:
    cloze = Note(client_id=client_id(7), fields=ClozeFields(text="{{c1::Paris}}"))

    with pytest.raises(DuplicateClientIdError):
        result_with(basic_note(7), cloze)


@pytest.mark.parametrize("length", [1, MAX_TITLE_LENGTH])
def test_deck_title_within_bounds_is_accepted(length: int) -> None:
    assert len(Deck(title="a" * length).title) == length


def test_deck_title_length_is_counted_in_utf16_code_units() -> None:
    assert Deck(title="\N{VERTICAL TRAFFIC LIGHT}" * (MAX_TITLE_LENGTH // 2)).title
    with pytest.raises(InvalidDeckError):
        Deck(title="\N{VERTICAL TRAFFIC LIGHT}" * (MAX_TITLE_LENGTH // 2 + 1))


def test_deck_description_length_is_counted_in_utf16_code_units() -> None:
    with pytest.raises(InvalidDeckError):
        Deck(title="Signs", description="\N{VERTICAL TRAFFIC LIGHT}" * (MAX_DESCRIPTION_LENGTH // 2 + 1))


def test_deck_title_over_the_limit_is_rejected() -> None:
    with pytest.raises(InvalidDeckError):
        Deck(title="a" * (MAX_TITLE_LENGTH + 1))


@pytest.mark.parametrize("title", ["", "   "])
def test_blank_deck_title_is_rejected(title: str) -> None:
    with pytest.raises(InvalidDeckError):
        Deck(title=title)


def test_deck_description_at_the_limit_is_accepted() -> None:
    assert Deck(title="Signs", description="a" * MAX_DESCRIPTION_LENGTH).description is not None


def test_deck_description_over_the_limit_is_rejected() -> None:
    with pytest.raises(InvalidDeckError):
        Deck(title="Signs", description="a" * (MAX_DESCRIPTION_LENGTH + 1))
