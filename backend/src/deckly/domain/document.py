import math
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
    is_web_url,
)

MAX_FILENAME_LENGTH = 255
PATH_SEPARATORS = ("/", "\\")
STORAGE_KEY_PREFIX = "documents"


class DocumentFormat(StrEnum):
    PDF = "application/pdf"
    PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    MP3 = "audio/mpeg"
    M4A = "audio/mp4"
    WAV = "audio/wav"
    MP4 = "video/mp4"
    MOV = "video/quicktime"
    TEXT = "text/plain"


RECORDING_FORMATS = frozenset(
    {DocumentFormat.MP3, DocumentFormat.M4A, DocumentFormat.WAV, DocumentFormat.MP4, DocumentFormat.MOV}
)


def is_recording(document_format: DocumentFormat) -> bool:
    return document_format in RECORDING_FORMATS


def is_article(document_format: DocumentFormat) -> bool:
    return document_format == DocumentFormat.TEXT


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
    duration_seconds: float | None = None
    source_url: str | None = None

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
        self._require_duration_only_for_recordings()
        self._require_source_url_only_for_articles()

    def _require_duration_only_for_recordings(self) -> None:
        if not is_recording(self.document_format):
            if self.duration_seconds is not None:
                message = "only a recording has a duration"
                raise InvalidDocumentError(message)
            return
        if self.duration_seconds is None or not math.isfinite(self.duration_seconds):
            message = "a recording must have a finite duration"
            raise InvalidDocumentError(message)
        if self.duration_seconds <= 0:
            message = "a recording must not be empty"
            raise InvalidDocumentError(message)

    def _require_source_url_only_for_articles(self) -> None:
        if not is_article(self.document_format):
            if self.source_url is not None:
                message = "only a page fetched from a url has a source url"
                raise InvalidDocumentError(message)
            return
        if self.source_url is None or not is_web_url(self.source_url):
            message = "a page fetched from a url must keep the http(s) url it was fetched from"
            raise InvalidDocumentError(message)

    @property
    def storage_key(self) -> str:
        return storage_key(self.document_id)
