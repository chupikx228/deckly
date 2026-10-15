from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from deckly.domain.exceptions import InvalidDocumentError, InvalidTimestampError
from deckly.domain.text import (
    is_blank,
    is_invisible_character,
    is_timezone_aware,
    is_unstorable_character,
    is_uuid_v4,
)

MAX_FILENAME_LENGTH = 255
PATH_SEPARATORS = ("/", "\\")
STORAGE_KEY_PREFIX = "documents"


class DocumentFormat(StrEnum):
    PDF = "application/pdf"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def last_path_component(raw: str) -> str:
    for separator in PATH_SEPARATORS:
        raw = raw.rpartition(separator)[2]
    return raw


def is_filename_character(character: str) -> bool:
    return not (is_unstorable_character(character) or is_invisible_character(character))


def clean_filename(raw: str) -> str:
    kept = "".join(filter(is_filename_character, last_path_component(raw)))
    cleaned = kept.strip()[:MAX_FILENAME_LENGTH].strip()
    if is_blank(cleaned):
        message = "the uploaded file has no usable name"
        raise InvalidDocumentError(message)
    return cleaned


def storage_key(document_id: UUID) -> str:
    return f"{STORAGE_KEY_PREFIX}/{document_id}"


@dataclass(frozen=True, slots=True)
class Document:
    document_id: UUID
    client_id: UUID
    filename: str
    size_bytes: int
    document_format: DocumentFormat
    uploaded_at: datetime

    def __post_init__(self) -> None:
        if not is_uuid_v4(self.document_id):
            message = f"documentId {self.document_id} is not a UUID v4"
            raise InvalidDocumentError(message)
        if self.filename != clean_filename(self.filename):
            message = "a document filename must already be clean"
            raise InvalidDocumentError(message)
        if self.size_bytes <= 0:
            message = "a document must not be empty"
            raise InvalidDocumentError(message)
        if not is_timezone_aware(self.uploaded_at):
            message = "document timestamps must carry an explicit offset"
            raise InvalidTimestampError(message)

    @property
    def storage_key(self) -> str:
        return storage_key(self.document_id)
