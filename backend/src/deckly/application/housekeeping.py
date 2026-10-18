import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from deckly.application.exceptions import JobNoLongerStaleError, UpstreamUnavailableError
from deckly.application.generations import Clock
from deckly.application.pipeline import log_context
from deckly.application.ports import (
    DocumentHousekeeping,
    DocumentStorage,
    ExpiredDocument,
    JobHousekeeping,
    JobStore,
    JobTransition,
    QueueLane,
)
from deckly.domain.job import FailureCode, GenerationJob, JobStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StaleCutoff:
    status: JobStatus
    lane: QueueLane
    updated_before: datetime


@dataclass(frozen=True, slots=True)
class StalenessPolicy:
    running_after: timedelta
    queued_after: timedelta
    recording_queued_after: timedelta

    def cutoffs(self, now: datetime) -> tuple[StaleCutoff, ...]:
        queued_after = {
            QueueLane.GENERATION: self.queued_after,
            QueueLane.TRANSCRIPTION: self.recording_queued_after,
        }
        return tuple(
            cutoff
            for lane in QueueLane
            for cutoff in (
                StaleCutoff(JobStatus.RUNNING, lane, now - self.running_after),
                StaleCutoff(JobStatus.QUEUED, lane, now - queued_after[lane]),
            )
        )


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    idempotency_key_ttl: timedelta
    job_retention: timedelta


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    released_keys: int
    purged_jobs: int


def fail_if_stale(status: JobStatus, cutoff: datetime, now: datetime) -> JobTransition:
    def transition(job: GenerationJob) -> GenerationJob:
        if job.status is not status or job.updated_at >= cutoff:
            message = f"job {job.job_id} moved on to {job.status} at {job.updated_at.isoformat()}"
            raise JobNoLongerStaleError(message)
        return job.fail(FailureCode.GENERATION_FAILED, now)

    return transition


@dataclass(frozen=True, slots=True)
class SweepStaleJobs:
    store: JobStore
    housekeeping: JobHousekeeping
    policy: StalenessPolicy
    batch_size: int
    clock: Clock

    async def __call__(self) -> int:
        now = self.clock()
        failed = 0
        for cutoff in self.policy.cutoffs(now):
            stale = await self.housekeeping.stale(
                cutoff.status, cutoff.lane, cutoff.updated_before, self.batch_size
            )
            for job_id in stale:
                failed += await self._fail(job_id, cutoff, now)
        return failed

    async def _fail(self, job_id: UUID, cutoff: StaleCutoff, now: datetime) -> int:
        context = {"job_id": str(job_id), "stale_status": cutoff.status, "lane": cutoff.lane}
        try:
            job = await self.store.update(job_id, fail_if_stale(cutoff.status, cutoff.updated_before, now))
        except JobNoLongerStaleError:
            logger.info("stale_job_moved_on", extra=context)
            return 0
        except Exception:
            logger.exception("stale_job_not_swept", extra=context)
            return 0
        if job is None:
            return 0
        logger.warning("stale_job_failed", extra={**log_context(job), **context})
        return 1


@dataclass(frozen=True, slots=True)
class EnforceJobRetention:
    housekeeping: JobHousekeeping
    policy: RetentionPolicy
    batch_size: int
    clock: Clock

    async def __call__(self) -> RetentionOutcome:
        now = self.clock()
        outcome = RetentionOutcome(
            released_keys=await self.housekeeping.release_idempotency_keys(
                now - self.policy.idempotency_key_ttl, self.batch_size
            ),
            purged_jobs=await self.housekeeping.purge_finished(
                now - self.policy.job_retention, self.batch_size
            ),
        )
        logger.info(
            "job_retention_enforced",
            extra={"released_keys": outcome.released_keys, "purged_jobs": outcome.purged_jobs},
        )
        return outcome


@dataclass(frozen=True, slots=True)
class DocumentRetentionPolicy:
    retention: timedelta
    batch_size: int
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class EnforceDocumentRetention:
    housekeeping: DocumentHousekeeping
    storage: DocumentStorage
    policy: DocumentRetentionPolicy
    clock: Clock

    async def __call__(self) -> int:
        uploaded_before = self.clock() - self.policy.retention
        async with self.housekeeping.expired(uploaded_before, self.policy.batch_size) as expired:
            slots = asyncio.Semaphore(self.policy.max_concurrency)
            deleted = await asyncio.gather(*(self._delete(document, slots) for document in expired.documents))
            removed = {
                document.document_id
                for document, gone in zip(expired.documents, deleted, strict=True)
                if gone
            }
            purged = await expired.purge(removed)
        logger.info(
            "document_retention_enforced",
            extra={"purged_documents": purged, "kept_documents": len(deleted) - purged},
        )
        return purged

    async def _delete(self, document: ExpiredDocument, slots: asyncio.Semaphore) -> bool:
        async with slots:
            try:
                await self.storage.delete(document.storage_key)
            except UpstreamUnavailableError as error:
                logger.warning(
                    "document_object_not_deleted",
                    extra={"document_id": str(document.document_id), "error": type(error).__name__},
                )
                return False
            except Exception:
                logger.exception(
                    "document_object_not_deleted", extra={"document_id": str(document.document_id)}
                )
                return False
        return True
