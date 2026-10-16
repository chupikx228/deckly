import io
import zipfile

import pytest

from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import UnsupportedDocumentError
from deckly.infrastructure.documents.archive import MAX_ARCHIVE_ENTRIES
from deckly.infrastructure.documents.sniffing import (
    MAX_CONTENT_TYPES_BYTES,
    PDF_HEADER_WINDOW,
    PDF_TRAILER_WINDOW,
    ContentDocumentInspector,
    detect_format,
)
from tests.document_files import MIB
from tests.documents import (
    MACRO_PRESENTATION_MAIN,
    SHEET_MAIN,
    SLIDESHOW_MAIN,
    WORD_MAIN,
    office_bytes,
    pdf_bytes,
    pptx_bytes,
)
from tests.memory import peak_memory

pytestmark = pytest.mark.anyio

OVERSIZED_ENTRY_COUNT = 200_000
OLE_COMPOUND_FILE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def rejected(content: bytes) -> None:
    with pytest.raises(UnsupportedDocumentError):
        detect_format(content)


@pytest.mark.parametrize("version", [b"1.0", b"1.4", b"1.7", b"2.0"])
def test_pdf_of_every_published_version_is_recognised(version: bytes) -> None:
    assert detect_format(pdf_bytes(header=b"%PDF-" + version + b"\n")) is DocumentFormat.PDF


def test_pdf_header_starting_at_the_last_offset_of_the_header_window_is_recognised() -> None:
    content = pdf_bytes(header=b" " * (PDF_HEADER_WINDOW - 1) + b"%PDF-1.7\n")

    assert detect_format(content) is DocumentFormat.PDF


def test_pdf_header_starting_just_past_the_header_window_is_rejected() -> None:
    rejected(pdf_bytes(header=b" " * PDF_HEADER_WINDOW + b"%PDF-1.7\n"))


@pytest.mark.parametrize("header", [b"%PDF-\n", b"%PDF-x.y\n", b"%PDF-3.0\n", b"%pdf-1.7\n", b"PDF-1.7\n"])
def test_malformed_pdf_header_is_rejected(header: bytes) -> None:
    rejected(pdf_bytes(header=header))


def test_pdf_without_an_end_of_file_marker_is_rejected_as_truncated() -> None:
    rejected(b"%PDF-1.7\n1 0 obj << >> endobj\n")


def test_pdf_whose_end_marker_sits_just_inside_the_trailer_window_is_recognised() -> None:
    content = b"%PDF-1.7\n%%EOF" + b" " * (PDF_TRAILER_WINDOW - len(b"%%EOF"))

    assert detect_format(content) is DocumentFormat.PDF


def test_pdf_whose_end_marker_sits_just_outside_the_trailer_window_is_rejected() -> None:
    rejected(b"%PDF-1.7\n%%EOF" + b" " * (PDF_TRAILER_WINDOW - len(b"%%EOF") + 1))


def test_text_file_named_like_a_pdf_is_rejected() -> None:
    rejected(b"This is not a PDF, whatever the file name says.\n")


def test_presentation_is_recognised() -> None:
    assert detect_format(pptx_bytes()) is DocumentFormat.PPTX


@pytest.mark.parametrize(
    ("main_type", "main_part"),
    [
        (MACRO_PRESENTATION_MAIN, "ppt/presentation.xml"),
        (SLIDESHOW_MAIN, "ppt/presentation.xml"),
        (WORD_MAIN, "word/document.xml"),
        (SHEET_MAIN, "xl/workbook.xml"),
    ],
    ids=["pptm", "ppsx", "docx", "xlsx"],
)
def test_other_office_packages_are_rejected(main_type: str, main_part: str) -> None:
    rejected(office_bytes(main_type=main_type, main_part=main_part))


def test_presentation_carrying_a_macro_project_is_rejected() -> None:
    rejected(office_bytes(extra={"ppt/vbaProject.bin": b"\x00" * 16}))


def test_zip_without_a_content_types_part_is_rejected() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/presentation.xml", b"<presentation/>")
    rejected(buffer.getvalue())


def test_content_types_part_over_its_size_cap_is_rejected_without_reading_it_whole() -> None:
    padding = b" " * MAX_CONTENT_TYPES_BYTES
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", padding + b"presentationml.presentation.main+xml")
        archive.writestr("ppt/presentation.xml", b"<presentation/>")
    rejected(buffer.getvalue())


def test_archive_with_too_many_entries_is_rejected_before_its_directory_is_read() -> None:
    padding = {f"ppt/media/empty{index}.bin": b"" for index in range(OVERSIZED_ENTRY_COUNT)}

    archive = office_bytes(extra=padding)

    with peak_memory() as peak:
        rejected(archive)

    assert peak[0] < MIB


def test_archive_at_the_entry_limit_is_still_recognised() -> None:
    padding = {f"ppt/media/empty{index}.bin": b"" for index in range(MAX_ARCHIVE_ENTRIES - 2)}

    assert detect_format(office_bytes(extra=padding)) is DocumentFormat.PPTX


def test_legacy_binary_powerpoint_is_rejected() -> None:
    rejected(OLE_COMPOUND_FILE)


@pytest.mark.parametrize(
    "content",
    [b"PK\x03\x04", b"PK\x03\x04" + b"\x00" * 64, pptx_bytes()[: len(pptx_bytes()) // 2]],
    ids=["signature only", "garbage after the signature", "cut in half"],
)
def test_damaged_zip_is_rejected(content: bytes) -> None:
    rejected(content)


def test_presentation_whose_content_types_part_fails_its_checksum_is_rejected() -> None:
    content = bytearray(pptx_bytes())
    marker = content.index(b"[Content_Types].xml") + len(b"[Content_Types].xml")
    content[marker + 4] ^= 0xFF
    rejected(bytes(content))


def test_empty_file_is_rejected() -> None:
    rejected(b"")


async def test_inspector_reports_the_detected_format() -> None:
    assert await ContentDocumentInspector().identify(pptx_bytes()) is DocumentFormat.PPTX


async def test_inspector_rejects_what_detection_rejects() -> None:
    with pytest.raises(UnsupportedDocumentError):
        await ContentDocumentInspector().identify(b"not a document")
