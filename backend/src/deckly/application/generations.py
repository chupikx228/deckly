import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from deckly.application.exceptions import (
    IdempotencyKeyConflictError,
    RateLimitedError,
    UnreadableJobRequestError,
)
from deckly.application.ports import (
    AdmissionOutcome,
    DocumentStore,
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
from deckly.domain.document import Document
from deckly.domain.exceptions import DocumentNotFoundError, JobNotFoundError, TopicRejectedError
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
    documents: DocumentStore
    clock: Clock
    new_job_id: JobIdFactory
    telemetry: GenerationTelemetry

    async def __call__(self, request: GenerationRequest, scope: IdempotencyScope, address: str) -> JobCreated:
        now = self.clock()
        existing = await self._find(scope)
        if existing is not None:
            return await self._replay(existing, request, scope, now)
        document = await self._document(request, scope.client_id)
        requester = Requester(client_id=scope.client_id, address=address)
        try:
            quota = await self.quota.reserve(requester, now)
        except RateLimitedError:
            twin = await self._find(scope)
            if twin is None:
                raise
            return await self._replay(twin, request, scope, now)
        if not await self._screen(request, document, requester, now):
            twin = await self._find(scope)
            if twin is None:
                logger.info("topic_rejected", extra={"source_kind": request.source_kind})
                self.telemetry.admitted(AdmissionOutcome.TOPIC_REJECTED, request.source_kind)
                message = "the request violates the content policy"
                raise TopicRejectedError(message)
            await self.quota.release(requester, now)
            return await self._replay(twin, request, scope, now)
        job = GenerationJob.queue(self.new_job_id(), now)
        try:
            stored = await self.store.add(job, request, scope)
        except DocumentNotFoundError:
            await self.quota.release(requester, now)
            self._document_missing(request)
            raise
        except Exception:
            await self.quota.release(requester, now)
            raise
        if stored.job.job_id != job.job_id:
            await self.quota.release(requester, now)
            return await self._replay(stored, request, scope, now)
        await self.queue.enqueue(job.job_id)
        self._admit(job, request, AdmissionOutcome.QUEUED)
        return JobCreated(job=job, quota=quota)

    async def _find(self, scope: IdempotencyScope) -> StoredJob | None:
        try:
            return await self.store.find(scope)
        except UnreadableJobRequestError as error:
            message = f"{scope} was first used for a request that no longer validates"
            raise IdempotencyKeyConflictError(message) from error

    async def _document(self, request: GenerationRequest, client_id: UUID) -> Document | None:
        if request.document_id is None:
            return None
        document = await self.documents.get(request.document_id, client_id)
        if document is None:
            self._document_missing(request)
            message = f"document {request.document_id} does not exist for this client"
            raise DocumentNotFoundError(message)
        return document

    def _document_missing(self, request: GenerationRequest) -> None:
        logger.info("generation_document_not_found", extra={"document_id": str(request.document_id)})
        self.telemetry.admitted(AdmissionOutcome.DOCUMENT_NOT_FOUND, request.source_kind)

    async def _screen(
        self, request: GenerationRequest, document: Document | None, requester: Requester, now: datetime
    ) -> bool:
        try:
            return await self.moderator.allows(request, document)
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
        self._admit(stored.job, request, AdmissionOutcome.REPLAYED)
        return JobCreated(job=stored.job, quota=quota)

    def _admit(self, job: GenerationJob, request: GenerationRequest, outcome: AdmissionOutcome) -> None:
        context: dict[str, object] = {
            "job_id": str(job.job_id),
            "admission": outcome,
            "status": job.status,
            "source_kind": request.source_kind,
        }
        if request.document_id is not None:
            context["document_id"] = str(request.document_id)
        logger.info("generation_admitted", extra=context)
        self.telemetry.admitted(outcome, request.source_kind)


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
