from collections.abc import Awaitable, Callable, Collection
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from deckly.application.progress import ProgressReport
from deckly.domain.deck import GenerationResult
from deckly.domain.document import Document, DocumentFormat
from deckly.domain.generation import Fingerprint, GenerationRequest, SourceKind
from deckly.domain.job import FailureCode, GenerationJob, JobStage, JobStatus
from deckly.domain.media import Media
from deckly.domain.notes.note import Note
from deckly.domain.recording_url import YouTubeVideo
from deckly.domain.regeneration import RegenerationRequest
from deckly.domain.source import Source, SourceQuery, WebSource

type JobTransition = Callable[[GenerationJob], GenerationJob]


class AdmissionOutcome(StrEnum):
    QUEUED = "queued"
    REPLAYED = "replayed"
    TOPIC_REJECTED = "topic_rejected"
    DOCUMENT_NOT_FOUND = "document_not_found"
    DOCUMENT_NOT_SUPPORTED = "document_not_supported"


class QueueLane(StrEnum):
    GENERATION = "generation"
    TRANSCRIPTION = "transcription"


class JobOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    client_id: UUID
    idempotency_key: UUID


@dataclass(frozen=True, slots=True)
class Requester:
    client_id: UUID
    address: str


@dataclass(frozen=True, slots=True)
class StoredJob:
    job: GenerationJob
    request: GenerationRequest
    client_id: UUID


@dataclass(frozen=True, slots=True)
class Quota:
    limit: int
    remaining: int
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class RetrievedPage:
    source: WebSource
    content: str


@dataclass(frozen=True, slots=True)
class SourceMaterial:
    source: Source
    text: str


@dataclass(frozen=True, slots=True)
class ImageQuery:
    client_id: UUID
    text: str


@dataclass(frozen=True, slots=True)
class GeneratedCards:
    result: GenerationResult
    image_queries: tuple[ImageQuery, ...] = ()


@dataclass(frozen=True, slots=True)
class NoteMedia:
    client_id: UUID
    media: Media


class MissingImageReason(StrEnum):
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MissingImage:
    client_id: UUID
    reason: MissingImageReason


@dataclass(frozen=True, slots=True)
class FetchedMedia:
    attachments: tuple[NoteMedia, ...] = ()
    missing: tuple[MissingImage, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentUpload:
    filename: str
    content: bytes = field(repr=False)


type UploadReader = Callable[[], Awaitable[DocumentUpload]]


@dataclass(frozen=True, slots=True)
class InspectedUpload:
    document_format: DocumentFormat
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ExpiredDocument:
    document_id: UUID
    storage_key: str


class JobStore(Protocol):
    async def add(
        self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope
    ) -> StoredJob: ...

    async def find(self, scope: IdempotencyScope) -> StoredJob | None: ...

    async def get(self, job_id: UUID) -> GenerationJob | None: ...

    async def get_stored(self, job_id: UUID) -> StoredJob | None: ...

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None: ...


class JobHousekeeping(Protocol):
    async def stale(
        self, status: JobStatus, lane: QueueLane, updated_before: datetime, limit: int
    ) -> tuple[UUID, ...]: ...

    async def release_idempotency_keys(self, created_before: datetime, limit: int) -> int: ...

    async def purge_finished(self, finished_before: datetime, limit: int) -> int: ...


class ResultCache(Protocol):
    async def get(self, fingerprint: Fingerprint) -> GenerationResult | None: ...

    async def put(self, fingerprint: Fingerprint, result: GenerationResult) -> None: ...


class JobQueue(Protocol):
    async def enqueue(self, job_id: UUID, lane: QueueLane) -> None: ...

    async def abort(self, job_id: UUID) -> None: ...


class QuotaReader(Protocol):
    async def current(self, client_id: UUID, now: datetime) -> Quota: ...


class GenerationQuota(QuotaReader, Protocol):
    async def reserve(self, requester: Requester, now: datetime) -> Quota: ...

    async def release(self, requester: Requester, now: datetime) -> None: ...


class DependencyProbe(Protocol):
    async def is_healthy(self) -> bool: ...


class SourceRetriever(Protocol):
    async def retrieve(self, job_id: UUID, query: SourceQuery) -> tuple[RetrievedPage, ...]: ...


class SourceParser(Protocol):
    async def parse(
        self, job_id: UUID, query: SourceQuery, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]: ...


class CardGenerator(Protocol):
    async def generate(
        self, job_id: UUID, request: GenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> GeneratedCards: ...


class TopicModerator(Protocol):
    async def allows(self, request: GenerationRequest, document: Document | None) -> bool: ...


class ContentModerator(Protocol):
    async def screen(self, job_id: UUID, subject: str, result: GenerationResult) -> GenerationResult: ...


class MaterialModerator(Protocol):
    async def allows(
        self, job_id: UUID, document: Document, material: tuple[SourceMaterial, ...]
    ) -> bool: ...


class MediaFetcher(Protocol):
    async def fetch(
        self, job_id: UUID, result: GenerationResult, queries: tuple[ImageQuery, ...]
    ) -> FetchedMedia: ...


class NoteRegenerator(Protocol):
    async def regenerate(
        self, request_id: UUID, request: RegenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> Note: ...


class RegenerationLimiter(Protocol):
    async def acquire(self, client_id: UUID, now: datetime) -> None: ...


class RegenerationModerator(Protocol):
    async def allows_request(self, request: RegenerationRequest) -> bool: ...

    async def allows_note(self, request_id: UUID, query: SourceQuery, note: Note) -> bool: ...


class UploadLimiter(Protocol):
    async def acquire(self, requester: Requester, now: datetime) -> None: ...


class UploadGate(Protocol):
    def admit(self) -> AbstractAsyncContextManager[None]: ...


class DocumentInspector(Protocol):
    async def identify(self, content: bytes) -> InspectedUpload: ...


class RecordingFetcher(Protocol):
    async def fetch(self, video: YouTubeVideo) -> DocumentUpload: ...


class DocumentStorage(Protocol):
    async def put(self, key: str, content: bytes, content_type: str) -> None: ...

    async def get(self, key: str, max_bytes: int) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class DocumentParser(Protocol):
    async def parse(
        self, document: Document, content: bytes, report: ProgressReport
    ) -> tuple[SourceMaterial, ...]: ...


class DocumentStore(Protocol):
    async def add_pending(self, document: Document) -> None: ...

    async def mark_stored(self, document_id: UUID) -> None: ...

    async def discard(self, document_id: UUID) -> None: ...

    async def get(self, document_id: UUID, client_id: UUID) -> Document | None: ...


class ExpiredDocuments(Protocol):
    @property
    def documents(self) -> tuple[ExpiredDocument, ...]: ...

    async def purge(self, document_ids: Collection[UUID]) -> int: ...


class DocumentHousekeeping(Protocol):
    def expired(
        self, uploaded_before: datetime, limit: int
    ) -> AbstractAsyncContextManager[ExpiredDocuments]: ...


class JobRun(Protocol):
    def identify(self, source: SourceKind) -> None: ...

    def finish(self, outcome: JobOutcome, code: FailureCode | None = None) -> None: ...


class GenerationTelemetry(Protocol):
    def admitted(self, outcome: AdmissionOutcome, source: SourceKind) -> None: ...

    def cancelled(self) -> None: ...

    def run(self, job_id: UUID) -> AbstractContextManager[JobRun]: ...

    def stage(self, stage: JobStage, source: SourceKind) -> AbstractContextManager[None]: ...
