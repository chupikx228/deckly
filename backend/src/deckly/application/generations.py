from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from deckly.application.exceptions import IdempotencyKeyConflictError, RateLimitedError
from deckly.application.ports import (
    GenerationQuota,
    IdempotencyScope,
    JobQueue,
    JobStore,
    Quota,
    Requester,
    StoredJob,
)
from deckly.domain.exceptions import JobNotFoundError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob, JobStatus

type Clock = Callable[[], datetime]
type JobIdFactory = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class JobCreated:
    job: GenerationJob
    quota: Quota


@dataclass(frozen=True, slots=True)
class CreateGeneration:
    store: JobStore
    queue: JobQueue
    quota: GenerationQuota
    clock: Clock
    new_job_id: JobIdFactory

    async def __call__(self, request: GenerationRequest, scope: IdempotencyScope, address: str) -> JobCreated:
        now = self.clock()
        existing = await self.store.find(scope)
        if existing is not None:
            return await self._replay(existing, request, scope, now)
        requester = Requester(client_id=scope.client_id, address=address)
        try:
            quota = await self.quota.reserve(requester, now)
        except RateLimitedError:
            twin = await self.store.find(scope)
            if twin is None:
                raise
            return await self._replay(twin, request, scope, now)
        job = GenerationJob.queue(self.new_job_id(), now)
        try:
            stored = await self.store.add(job, request, scope)
        except Exception:
            await self.quota.release(requester, now)
            raise
        if stored.job.job_id != job.job_id:
            await self.quota.release(requester, now)
            return await self._replay(stored, request, scope, now)
        await self.queue.enqueue(job.job_id)
        return JobCreated(job=job, quota=quota)

    async def _replay(
        self, stored: StoredJob, request: GenerationRequest, scope: IdempotencyScope, now: datetime
    ) -> JobCreated:
        if stored.request != request:
            message = f"{scope} was first used for a different request"
            raise IdempotencyKeyConflictError(message)
        if stored.job.status is JobStatus.QUEUED:
            await self.queue.enqueue(stored.job.job_id)
        return JobCreated(job=stored.job, quota=await self.quota.current(scope.client_id, now))


@dataclass(frozen=True, slots=True)
class GetGeneration:
    store: JobStore

    async def __call__(self, job_id: UUID) -> GenerationJob:
        job = await self.store.get(job_id)
        if job is None:
            message = f"job {job_id} does not exist"
            raise JobNotFoundError(message)
        return job


@dataclass(frozen=True, slots=True)
class CancelGeneration:
    store: JobStore
    queue: JobQueue
    clock: Clock

    async def __call__(self, job_id: UUID) -> GenerationJob:
        now = self.clock()
        job = await self.store.update(job_id, lambda stored: stored.cancel(now))
        if job is None:
            message = f"job {job_id} does not exist"
            raise JobNotFoundError(message)
        await self.queue.abort(job_id)
        return job
