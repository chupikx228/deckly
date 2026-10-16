import io
import struct
import zlib
from collections.abc import Generator
from itertools import islice

from pypdf import PageObject, PasswordType, PdfReader, apply_configuration
from pypdf.errors import DependencyError, PyPdfError

from deckly.application.exceptions import EncryptedDocumentError, UnreadableDocumentError
from deckly.infrastructure.documents.extraction import MAX_EXTRACTED_UNITS, ExtractedUnit, ExtractionDeadline

MAX_DECODED_STREAM_BYTES = 16 * 1024 * 1024
MAX_DECODED_CONTENT_BYTES = 64 * 1024 * 1024
MAX_FORM_INVOCATIONS_PER_PAGE = 500
MAX_PAGE_TREE_ENTRIES = 5_000
NO_PASSWORD = ""
UNREADABLE_PDF_ERRORS = (
    PyPdfError,
    ArithmeticError,
    AttributeError,
    EOFError,
    LookupError,
    OSError,
    RecursionError,
    TypeError,
    ValueError,
    struct.error,
    zlib.error,
)


def open_reader(content: bytes) -> PdfReader:
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted and reader.decrypt(NO_PASSWORD) is PasswordType.NOT_DECRYPTED:
        message = "the PDF needs a password to open"
        raise EncryptedDocumentError(message)
    return reader


def decoded_content_bytes(page: PageObject) -> int:
    contents = page.get_contents()
    return 0 if contents is None else len(contents.get_data())


def read_pages(content: bytes, deadline: ExtractionDeadline) -> Generator[ExtractedUnit]:
    reader = open_reader(content)
    decoded = 0
    for number, page in enumerate(islice(reader.pages, MAX_EXTRACTED_UNITS), start=1):
        deadline.check()
        decoded += decoded_content_bytes(page)
        if decoded > MAX_DECODED_CONTENT_BYTES:
            message = f"the PDF's page content inflates past {MAX_DECODED_CONTENT_BYTES} bytes"
            raise UnreadableDocumentError(message)
        yield ExtractedUnit(
            number=number, text=page.extract_text(visitor_operand_before=deadline.check_operation)
        )


def pdf_pages(content: bytes, deadline: ExtractionDeadline) -> Generator[ExtractedUnit]:
    with apply_configuration(
        zlib_maximum_output_length=MAX_DECODED_STREAM_BYTES,
        lzw_maximum_output_length=MAX_DECODED_STREAM_BYTES,
        run_length_maximum_output_length=MAX_DECODED_STREAM_BYTES,
        jbig2_maximum_output_length=MAX_DECODED_STREAM_BYTES,
        brotli_maximum_output_length=MAX_DECODED_STREAM_BYTES,
        array_based_stream_maximum_output_length=MAX_DECODED_STREAM_BYTES,
        xform_maximum_invocations_per_extraction=MAX_FORM_INVOCATIONS_PER_PAGE,
        page_tree_maximum_entries=MAX_PAGE_TREE_ENTRIES,
    ):
        try:
            yield from read_pages(content, deadline)
        except DependencyError as error:
            message = "the PDF is encrypted with a cipher this service does not decrypt"
            raise EncryptedDocumentError(message) from error
        except UNREADABLE_PDF_ERRORS as error:
            message = f"the PDF could not be read: {type(error).__name__}"
            raise UnreadableDocumentError(message) from error
