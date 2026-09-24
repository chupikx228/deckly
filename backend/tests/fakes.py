from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from deckly.application.generations import CreateGeneration
from deckly.application.ports import IdempotencyScope, Quota, StoredJob
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import GenerationJob
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.quota import QUOTA_WINDOW
from tests.domain.builders import T0

QUOTA_LIMIT = 20


def job_id(number: int) -> UUID:
    return UUID(int=number, version=4)


def sequential_job_ids() -> Iterator[UUID]:
    number = 1
    while True:
        yield job_id(number)
        number += 1


def generation_request(topic: str = "Road signs") -> GenerationRequest:
    return GenerationRequest(
        topic=topic,
        language="ru",
        card_count=40,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=(NoteType.BASIC,),
        include_images=False,
        instructions=None,
    )


def scope(client: int = 1, key: int = 1) -> IdempotencyScope:
    return IdempotencyScope(client_id=UUID(int=client, version=4), idempotency_key=UUID(int=key, version=4))


class InMemoryJobStore:
    def __init__(self) -> None:
        self.jobs: dict[UUID, GenerationJob] = {}
        self.requests: dict[UUID, GenerationRequest] = {}
        self.job_ids_by_scope: dict[IdempotencyScope, UUID] = {}

    async def add(self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope) -> StoredJob:
        existing = self.job_ids_by_scope.get(scope)
        if existing is not None:
            return StoredJob(job=self.jobs[existing], request=self.requests[existing])
        self.job_ids_by_scope[scope] = job.job_id
        self.jobs[job.job_id] = job
        self.requests[job.job_id] = request
        return StoredJob(job=job, request=request)

    def replace(self, job: GenerationJob) -> None:
        self.jobs[job.job_id] = job


class RecordingJobQueue:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []
        self.unavailable = False

    async def enqueue(self, job_id: UUID) -> None:
        if self.unavailable:
            message = "queue unavailable"
            raise ConnectionError(message)
        self.enqueued.append(job_id)


class FixedQuota:
    async def current(self, client_id: UUID, now: datetime) -> Quota:
        del client_id
        return Quota(limit=QUOTA_LIMIT, remaining=QUOTA_LIMIT, resets_at=now + QUOTA_WINDOW)


class Harness:
    def __init__(self) -> None:
        self.store = InMemoryJobStore()
        self.queue = RecordingJobQueue()
        self.now = T0
        ids = sequential_job_ids()
        self.create = CreateGeneration(
            store=self.store,
            queue=self.queue,
            quota=FixedQuota(),
            clock=lambda: self.now,
            new_job_id=lambda: next(ids),
        )
