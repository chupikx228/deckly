from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from deckly.domain.deck import GenerationResult
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob, JobStatus
from deckly.domain.media import Media
from deckly.domain.notes.note import Note
from deckly.domain.regeneration import RegenerationRequest
from deckly.domain.source import Source

type JobTransition = Callable[[GenerationJob], GenerationJob]


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


@dataclass(frozen=True, slots=True)
class Quota:
    limit: int
    remaining: int
    resets_at: datetime


@dataclass(frozen=True, slots=True)
class RetrievedPage:
    source: Source
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


class JobStore(Protocol):
    async def add(
        self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope
    ) -> StoredJob: ...

    async def find(self, scope: IdempotencyScope) -> StoredJob | None: ...

    async def get(self, job_id: UUID) -> GenerationJob | None: ...

    async def get_stored(self, job_id: UUID) -> StoredJob | None: ...

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None: ...


class JobHousekeeping(Protocol):
    async def stale(self, status: JobStatus, updated_before: datetime, limit: int) -> tuple[UUID, ...]: ...

    async def release_idempotency_keys(self, created_before: datetime, limit: int) -> int: ...

    async def purge_finished(self, finished_before: datetime, limit: int) -> int: ...


class ResultCache(Protocol):
    async def get(self, request: GenerationRequest) -> GenerationResult | None: ...

    async def put(self, request: GenerationRequest, result: GenerationResult) -> None: ...


class JobQueue(Protocol):
    async def enqueue(self, job_id: UUID) -> None: ...

    async def abort(self, job_id: UUID) -> None: ...


class QuotaReader(Protocol):
    async def current(self, client_id: UUID, now: datetime) -> Quota: ...


class GenerationQuota(QuotaReader, Protocol):
    async def reserve(self, requester: Requester, now: datetime) -> Quota: ...

    async def release(self, requester: Requester, now: datetime) -> None: ...


class DependencyProbe(Protocol):
    async def is_healthy(self) -> bool: ...


class SourceRetriever(Protocol):
    async def retrieve(self, job_id: UUID, request: GenerationRequest) -> tuple[RetrievedPage, ...]: ...


class SourceParser(Protocol):
    async def parse(
        self, job_id: UUID, request: GenerationRequest, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]: ...


class CardGenerator(Protocol):
    async def generate(
        self, job_id: UUID, request: GenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> GeneratedCards: ...


class TopicModerator(Protocol):
    async def allows(self, request: GenerationRequest) -> bool: ...


class ContentModerator(Protocol):
    async def screen(
        self, job_id: UUID, request: GenerationRequest, result: GenerationResult
    ) -> GenerationResult: ...


class MediaFetcher(Protocol):
    async def fetch(self, job_id: UUID, queries: tuple[ImageQuery, ...]) -> tuple[NoteMedia, ...]: ...


class NoteRegenerator(Protocol):
    async def regenerate(
        self, request_id: UUID, request: RegenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> Note: ...


class RegenerationLimiter(Protocol):
    async def acquire(self, client_id: UUID, now: datetime) -> None: ...
