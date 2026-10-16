from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from deckly.domain.exceptions import InvalidGenerationRequestError, InvalidSourceError
from deckly.domain.text import is_blank, is_timezone_aware, is_uuid_v4, is_web_url

FIRST_CITATION_NUMBER = 1


@dataclass(frozen=True, slots=True)
class SourceQuery:
    topic: str
    language: str

    def __post_init__(self) -> None:
        if is_blank(self.topic):
            message = "topic must contain visible characters"
            raise InvalidGenerationRequestError(message)


def require_title(title: str) -> None:
    if is_blank(title):
        message = "source title must not be blank"
        raise InvalidSourceError(message)


@dataclass(frozen=True, slots=True)
class WebSource:
    title: str
    url: str
    retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        require_title(self.title)
        if not is_web_url(self.url):
            message = f"source url {self.url!r} is not an http(s) url"
            raise InvalidSourceError(message)
        if self.retrieved_at is not None and not is_timezone_aware(self.retrieved_at):
            message = "source retrievedAt must carry an explicit offset"
            raise InvalidSourceError(message)


class CitationUnit(StrEnum):
    PAGE = "page"
    SLIDE = "slide"


@dataclass(frozen=True, slots=True)
class DocumentSource:
    title: str
    document_id: UUID
    unit: CitationUnit
    number: int

    def __post_init__(self) -> None:
        require_title(self.title)
        if not is_uuid_v4(self.document_id):
            message = f"source documentId {self.document_id} is not a UUID v4"
            raise InvalidSourceError(message)
        if isinstance(self.number, bool) or self.number < FIRST_CITATION_NUMBER:
            message = f"source {self.unit} number must be at least {FIRST_CITATION_NUMBER}, got {self.number}"
            raise InvalidSourceError(message)


type Source = WebSource | DocumentSource
