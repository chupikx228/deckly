import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from deckly.application.exceptions import JobNoLongerStaleError
from deckly.application.generations import Clock
from deckly.application.pipeline import log_context
from deckly.application.ports import JobHousekeeping, JobStore, JobTransition
from deckly.domain.job import FailureCode, GenerationJob, JobStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StalenessPolicy:
    running_after: timedelta
    queued_after: timedelta

    def cutoffs(self, now: datetime) -> Mapping[JobStatus, datetime]:
        return {JobStatus.RUNNING: now - self.running_after, JobStatus.QUEUED: now - self.queued_after}


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
        for status, cutoff in self.policy.cutoffs(now).items():
            for job_id in await self.housekeeping.stale(status, cutoff, self.batch_size):
                failed += await self._fail(job_id, status, cutoff, now)
        return failed

    async def _fail(self, job_id: UUID, status: JobStatus, cutoff: datetime, now: datetime) -> int:
        try:
            job = await self.store.update(job_id, fail_if_stale(status, cutoff, now))
        except JobNoLongerStaleError:
            logger.info("stale_job_moved_on", extra={"job_id": str(job_id), "stale_status": status})
            return 0
        except Exception:
            logger.exception("stale_job_not_swept", extra={"job_id": str(job_id), "stale_status": status})
            return 0
        if job is None:
            return 0
        logger.warning("stale_job_failed", extra={**log_context(job), "stale_status": status})
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
