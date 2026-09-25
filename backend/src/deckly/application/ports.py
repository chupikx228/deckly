from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from deckly.domain.deck import GenerationResult
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob
from deckly.domain.notes.note import Note
from deckly.domain.source import Source

type JobTransition = Callable[[GenerationJob], GenerationJob]


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    client_id: UUID
    idempotency_key: UUID


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


class JobStore(Protocol):
    async def add(
        self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope
    ) -> StoredJob: ...

    async def get(self, job_id: UUID) -> GenerationJob | None: ...

    async def get_stored(self, job_id: UUID) -> StoredJob | None: ...

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None: ...


class JobQueue(Protocol):
    async def enqueue(self, job_id: UUID) -> None: ...


class QuotaReader(Protocol):
    async def current(self, client_id: UUID, now: datetime) -> Quota: ...


class SourceRetriever(Protocol):
    async def retrieve(self, request: GenerationRequest) -> tuple[RetrievedPage, ...]: ...


class SourceParser(Protocol):
    async def parse(
        self, request: GenerationRequest, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]: ...


class CardGenerator(Protocol):
    async def generate(
        self, request: GenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> GenerationResult: ...


class MediaFetcher(Protocol):
    async def fetch(self, request: GenerationRequest, notes: tuple[Note, ...]) -> tuple[Note, ...]: ...
