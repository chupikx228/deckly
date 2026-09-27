import pytest

from deckly.domain.exceptions import InvalidGenerationRequestError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.regeneration import RegenerationRequest, RejectionReason
from tests.domain.builders import unregister

REGENERATABLE_TYPES = [note_type for note_type in NoteType if note_type is not NoteType.IMAGE_OCCLUSION]


def regeneration(
    note_type: NoteType = NoteType.BASIC,
    *,
    topic: str = "Road signs",
    reason: RejectionReason = RejectionReason.TOO_EASY,
) -> RegenerationRequest:
    return RegenerationRequest(
        topic=topic,
        language="ru",
        note_type=note_type,
        rejected_fields={"front": "Red triangle?", "back": "A warning"},
        reason=reason,
    )


@pytest.mark.parametrize("note_type", REGENERATABLE_TYPES)
def test_every_note_type_built_without_an_image_can_be_regenerated(note_type: NoteType) -> None:
    assert regeneration(note_type).note_type is note_type


@pytest.mark.parametrize("reason", list(RejectionReason))
def test_every_rejection_reason_is_accepted(reason: RejectionReason) -> None:
    assert regeneration(reason=reason).reason is reason


def test_rejection_reasons_match_the_contract() -> None:
    assert [str(reason) for reason in RejectionReason] == [
        "too_easy",
        "too_hard",
        "incorrect",
        "duplicate",
        "off_topic",
        "other",
    ]


def test_image_occlusion_cannot_be_regenerated_without_an_image() -> None:
    with pytest.raises(InvalidGenerationRequestError, match="image"):
        regeneration(NoteType.IMAGE_OCCLUSION)


def test_note_type_without_a_field_shape_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    unregister(monkeypatch, NoteType.CLOZE)

    with pytest.raises(InvalidGenerationRequestError, match="cannot be regenerated"):
        regeneration(NoteType.CLOZE)


@pytest.mark.parametrize("topic", ["", "   ", "\N{ZERO WIDTH SPACE}\N{ZERO WIDTH JOINER}"])
def test_blank_topic_is_rejected(topic: str) -> None:
    with pytest.raises(InvalidGenerationRequestError, match="topic"):
        regeneration(topic=topic)
