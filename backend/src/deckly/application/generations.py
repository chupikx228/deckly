import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from deckly.application.exceptions import IdempotencyKeyConflictError, RateLimitedError
from deckly.application.ports import (
    AdmissionOutcome,
    GenerationQuota,
    GenerationTelemetry,
    IdempotencyScope,
    JobQueue,
    JobStore,
    Quota,
    Requester,
    StoredJob,
    TopicModerator,
)
from deckly.domain.exceptions import JobNotFoundError, TopicRejectedError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob, JobStatus

logger = logging.getLogger(__name__)

MILLISECONDS_PER_SECOND = 1000

type Clock = Callable[[], datetime]
type JobIdFactory = Callable[[], UUID]


def elapsed_ms(started: datetime, finished: datetime) -> int:
    return max(0, round((finished - started).total_seconds() * MILLISECONDS_PER_SECOND))


@dataclass(frozen=True, slots=True)
class JobCreated:
    job: GenerationJob
    quota: Quota


@dataclass(frozen=True, slots=True)
class CreateGeneration:
    store: JobStore
    queue: JobQueue
    quota: GenerationQuota
    moderator: TopicModerator
    clock: Clock
    new_job_id: JobIdFactory
    telemetry: GenerationTelemetry

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
        if not await self._screen(request, requester, now):
            twin = await self.store.find(scope)
            if twin is None:
                logger.info("topic_rejected")
                self.telemetry.admitted(AdmissionOutcome.TOPIC_REJECTED)
                message = "topic violates the content policy"
                raise TopicRejectedError(message)
            await self.quota.release(requester, now)
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
        self._admit(job, AdmissionOutcome.QUEUED)
        return JobCreated(job=job, quota=quota)

    async def _screen(self, request: GenerationRequest, requester: Requester, now: datetime) -> bool:
        try:
            return await self.moderator.allows(request)
        except BaseException:
            await self.quota.release(requester, now)
            raise

    async def _replay(
        self, stored: StoredJob, request: GenerationRequest, scope: IdempotencyScope, now: datetime
    ) -> JobCreated:
        if stored.request != request:
            message = f"{scope} was first used for a different request"
            raise IdempotencyKeyConflictError(message)
        if stored.job.status is JobStatus.QUEUED:
            await self.queue.enqueue(stored.job.job_id)
        quota = await self.quota.current(scope.client_id, now)
        self._admit(stored.job, AdmissionOutcome.REPLAYED)
        return JobCreated(job=stored.job, quota=quota)

    def _admit(self, job: GenerationJob, outcome: AdmissionOutcome) -> None:
        logger.info(
            "generation_admitted",
            extra={"job_id": str(job.job_id), "admission": outcome, "status": job.status},
        )
        self.telemetry.admitted(outcome)


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
    telemetry: GenerationTelemetry

    async def __call__(self, job_id: UUID) -> GenerationJob:
        now = self.clock()
        job = await self.store.update(job_id, lambda stored: stored.cancel(now))
        if job is None:
            message = f"job {job_id} does not exist"
            raise JobNotFoundError(message)
        logger.info(
            "generation_cancelled",
            extra={"job_id": str(job_id), "stage": job.stage, "progress": job.progress.value},
        )
        self.telemetry.cancelled()
        await self.queue.abort(job_id)
        return job
