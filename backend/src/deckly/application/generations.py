from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from deckly.application.exceptions import IdempotencyKeyConflictError
from deckly.application.ports import IdempotencyScope, JobQueue, JobStore, Quota, QuotaReader
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
    quota: QuotaReader
    clock: Clock
    new_job_id: JobIdFactory

    async def __call__(self, request: GenerationRequest, scope: IdempotencyScope) -> JobCreated:
        now = self.clock()
        stored = await self.store.add(GenerationJob.queue(self.new_job_id(), now), request, scope)
        if stored.request != request:
            message = f"{scope} was first used for a different request"
            raise IdempotencyKeyConflictError(message)
        if stored.job.status is JobStatus.QUEUED:
            await self.queue.enqueue(stored.job.job_id)
        return JobCreated(job=stored.job, quota=await self.quota.current(scope.client_id, now))
