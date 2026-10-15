import asyncio
import io
import re
import zipfile
import zlib

from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import UnsupportedDocumentError

PDF_HEADER = re.compile(rb"%PDF-[12]\.[0-9]")
PDF_HEADER_WINDOW = 1024
PDF_HEADER_LENGTH = len(b"%PDF-1.7")
PDF_END_MARKER = b"%%EOF"
PDF_TRAILER_WINDOW = 1024
ZIP_SIGNATURE = b"PK\x03\x04"
CONTENT_TYPES_ENTRY = "[Content_Types].xml"
PRESENTATION_ENTRY = "ppt/presentation.xml"
MACRO_ENTRY = "ppt/vbaProject.bin"
PRESENTATION_MAIN_TYPE = b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
MAX_CONTENT_TYPES_BYTES = 1024 * 1024
UNREADABLE_ARCHIVE_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    EOFError,
    OSError,
    RuntimeError,
    ValueError,
    zlib.error,
)


def is_pdf(content: bytes) -> bool:
    header = PDF_HEADER.search(content, 0, PDF_HEADER_WINDOW - 1 + PDF_HEADER_LENGTH)
    return header is not None and PDF_END_MARKER in content[-PDF_TRAILER_WINDOW:]


def declared_content_types(archive: zipfile.ZipFile) -> bytes | None:
    entry = archive.getinfo(CONTENT_TYPES_ENTRY)
    if entry.file_size > MAX_CONTENT_TYPES_BYTES:
        return None
    with archive.open(entry) as stream:
        declared = stream.read(MAX_CONTENT_TYPES_BYTES + 1)
    return None if len(declared) > MAX_CONTENT_TYPES_BYTES else declared


def presentation_content_types(content: bytes) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = frozenset(archive.namelist())
        if CONTENT_TYPES_ENTRY not in names or PRESENTATION_ENTRY not in names or MACRO_ENTRY in names:
            return None
        return declared_content_types(archive)


def is_pptx(content: bytes) -> bool:
    if not content.startswith(ZIP_SIGNATURE):
        return False
    try:
        declared = presentation_content_types(content)
    except UNREADABLE_ARCHIVE_ERRORS:
        return False
    return declared is not None and PRESENTATION_MAIN_TYPE in declared


def detect_format(content: bytes) -> DocumentFormat:
    if is_pdf(content):
        return DocumentFormat.PDF
    if is_pptx(content):
        return DocumentFormat.PPTX
    message = "the uploaded file is neither a PDF nor a PowerPoint presentation (.pptx)"
    raise UnsupportedDocumentError(message)


class ContentDocumentInspector:
    async def identify(self, content: bytes) -> DocumentFormat:
        return await asyncio.to_thread(detect_format, content)
