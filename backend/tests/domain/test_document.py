from dataclasses import replace
from uuid import UUID

import pytest

from deckly.domain.document import (
    MAX_FILENAME_LENGTH,
    RECORDING_FORMATS,
    DocumentFormat,
    clean_filename,
    is_recording,
)
from deckly.domain.exceptions import InvalidDocumentError, InvalidTimestampError
from tests.documents import document
from tests.domain.builders import T0


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("lecture.pdf", "lecture.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\slides.pptx", "slides.pptx"),
        ("folder/sub\\mixed.pdf", "mixed.pdf"),
        ("  spaced name.pdf  ", "spaced name.pdf"),
        ("report\u202efdp.exe", "reportfdp.exe"),
        ("nul\x00byte.pdf", "nulbyte.pdf"),
        ("zero\u200bwidth.pdf", "zerowidth.pdf"),
        ("new\nline.pdf", "newline.pdf"),
        ("лекция №3.pdf", "лекция №3.pdf"),
    ],
)
def test_filename_keeps_only_its_visible_last_path_component(raw: str, cleaned: str) -> None:
    assert clean_filename(raw) == cleaned


@pytest.mark.parametrize(
    "raw", ["", "   ", "folder/", "\u200b\u202e", "\x00", "a/b/\t", "\u0301", "\u00a0\u3164\u00a0"]
)
def test_filename_with_nothing_visible_left_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidDocumentError):
        clean_filename(raw)


def test_filename_at_the_length_limit_is_kept_whole() -> None:
    name = "a" * MAX_FILENAME_LENGTH

    assert clean_filename(name) == name


def test_filename_over_the_length_limit_is_cut_to_it() -> None:
    assert clean_filename("b" * (MAX_FILENAME_LENGTH + 1)) == "b" * MAX_FILENAME_LENGTH


def test_filename_cut_at_the_limit_does_not_end_in_whitespace() -> None:
    cleaned = clean_filename("c" * (MAX_FILENAME_LENGTH - 1) + " tail.pdf")

    assert cleaned == "c" * (MAX_FILENAME_LENGTH - 1)


def test_storage_key_is_derived_from_the_id_never_from_the_filename() -> None:
    stored = replace(document(1), filename="..")

    assert stored.storage_key == f"documents/{stored.document_id}"


@pytest.mark.parametrize("size_bytes", [0, -1])
def test_empty_document_is_rejected(size_bytes: int) -> None:
    with pytest.raises(InvalidDocumentError):
        replace(document(1), size_bytes=size_bytes)


def test_one_byte_document_is_accepted() -> None:
    assert replace(document(1), size_bytes=1).size_bytes == 1


def test_document_id_must_be_a_v4_uuid() -> None:
    with pytest.raises(InvalidDocumentError):
        replace(document(1), document_id=UUID("00000000-0000-1000-8000-000000000001"))


def test_document_filename_must_already_be_clean() -> None:
    with pytest.raises(InvalidDocumentError):
        replace(document(1), filename="dir/lecture.pdf")


def test_document_timestamp_needs_an_offset() -> None:
    with pytest.raises(InvalidTimestampError):
        replace(document(1), uploaded_at=T0.replace(tzinfo=None))


@pytest.mark.parametrize("document_format", sorted(RECORDING_FORMATS))
def test_recording_keeps_its_duration(document_format: DocumentFormat) -> None:
    recording = replace(document(1), document_format=document_format, duration_seconds=12.5)

    assert is_recording(recording.document_format)
    assert recording.duration_seconds == 12.5


@pytest.mark.parametrize("document_format", [DocumentFormat.PDF, DocumentFormat.PPTX])
def test_document_is_not_a_recording_and_has_no_duration(document_format: DocumentFormat) -> None:
    assert not is_recording(document_format)
    with pytest.raises(InvalidDocumentError, match="only a recording has a duration"):
        replace(document(1), document_format=document_format, duration_seconds=1.0)


@pytest.mark.parametrize("duration", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_recording_without_a_positive_finite_duration_is_rejected(duration: float | None) -> None:
    with pytest.raises(InvalidDocumentError):
        replace(document(1), document_format=DocumentFormat.MP3, duration_seconds=duration)
