from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob


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


class JobStore(Protocol):
    async def add(
        self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope
    ) -> StoredJob: ...

    async def get(self, job_id: UUID) -> GenerationJob | None: ...


class JobQueue(Protocol):
    async def enqueue(self, job_id: UUID) -> None: ...


class QuotaReader(Protocol):
    async def current(self, client_id: UUID, now: datetime) -> Quota: ...
