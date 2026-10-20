from collections.abc import Generator

from deckly.application.exceptions import UnreadableDocumentError
from deckly.domain.source import FIRST_CITATION_NUMBER
from deckly.infrastructure.documents.extraction import ExtractedUnit, ExtractionDeadline


def plain_text(content: bytes, deadline: ExtractionDeadline) -> Generator[ExtractedUnit]:
    deadline.check()
    try:
        text = content.decode()
    except UnicodeDecodeError as error:
        message = "the stored page text is not UTF-8"
        raise UnreadableDocumentError(message) from error
    yield ExtractedUnit(number=FIRST_CITATION_NUMBER, text=text)
