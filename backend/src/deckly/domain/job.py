from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Self
from uuid import UUID

from deckly.domain.deck import GenerationResult
from deckly.domain.exceptions import (
    InvalidJobIdError,
    InvalidJobTransitionError,
    InvalidProgressError,
    InvalidTimestampError,
    JobAlreadyTerminalError,
    ProgressRegressionError,
    StageRegressionError,
)
from deckly.domain.text import is_timezone_aware, is_uuid_v4


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class JobStage(StrEnum):
    PLANNING = "planning"
    RETRIEVING_SOURCES = "retrieving_sources"
    PARSING_SOURCES = "parsing_sources"
    GENERATING_CARDS = "generating_cards"
    FETCHING_MEDIA = "fetching_media"
    FINALIZING = "finalizing"

    @property
    def position(self) -> int:
        return STAGE_ORDER.index(self)


STAGE_ORDER: tuple[JobStage, ...] = tuple(JobStage)


@dataclass(frozen=True, slots=True)
class Progress:
    value: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            message = f"progress must be within 0.0-1.0, got {self.value}"
            raise InvalidProgressError(message)

    def advance_to(self, value: float) -> "Progress":
        advanced = Progress(value)
        if advanced.value < self.value:
            message = f"progress must never decrease, {self.value} -> {advanced.value}"
            raise ProgressRegressionError(message)
        return advanced


NO_PROGRESS = Progress(0.0)
COMPLETE = Progress(1.0)


@dataclass(frozen=True, slots=True)
class Queued:
    status: ClassVar[JobStatus] = JobStatus.QUEUED
    stage: ClassVar[None] = None
    progress: ClassVar[Progress] = NO_PROGRESS


@dataclass(frozen=True, slots=True)
class Running:
    status: ClassVar[JobStatus] = JobStatus.RUNNING
    stage: JobStage
    progress: Progress


@dataclass(frozen=True, slots=True)
class Succeeded:
    status: ClassVar[JobStatus] = JobStatus.SUCCEEDED
    stage: ClassVar[None] = None
    progress: ClassVar[Progress] = COMPLETE
    result: GenerationResult


@dataclass(frozen=True, slots=True)
class Failed:
    status: ClassVar[JobStatus] = JobStatus.FAILED
    reason: str
    stage: JobStage | None
    progress: Progress


@dataclass(frozen=True, slots=True)
class Cancelled:
    status: ClassVar[JobStatus] = JobStatus.CANCELLED
    stage: JobStage | None
    progress: Progress


type JobState = Queued | Running | Succeeded | Failed | Cancelled


@dataclass(frozen=True, slots=True)
class GenerationJob:
    job_id: UUID
    created_at: datetime
    updated_at: datetime
    state: JobState

    def __post_init__(self) -> None:
        if not is_uuid_v4(self.job_id):
            message = f"jobId {self.job_id} is not a UUID v4"
            raise InvalidJobIdError(message)
        if not (is_timezone_aware(self.created_at) and is_timezone_aware(self.updated_at)):
            message = "job timestamps must carry an explicit offset"
            raise InvalidTimestampError(message)
        if self.updated_at < self.created_at:
            message = "job updatedAt must not precede createdAt"
            raise InvalidTimestampError(message)

    @classmethod
    def queue(cls, job_id: UUID, now: datetime) -> Self:
        return cls(job_id=job_id, created_at=now, updated_at=now, state=Queued())

    @property
    def status(self) -> JobStatus:
        return self.state.status

    @property
    def stage(self) -> JobStage | None:
        return self.state.stage

    @property
    def progress(self) -> Progress:
        return self.state.progress

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def start(self, now: datetime) -> Self:
        self._require_status("start", JobStatus.QUEUED)
        return self._transition(Running(stage=JobStage.PLANNING, progress=NO_PROGRESS), now)

    def advance(self, stage: JobStage, progress: float, now: datetime) -> Self:
        running = self._require_running("advance")
        if stage.position < running.stage.position:
            message = f"stage must never move backwards, {running.stage} -> {stage}"
            raise StageRegressionError(message)
        return self._transition(Running(stage=stage, progress=running.progress.advance_to(progress)), now)

    def succeed(self, result: GenerationResult, now: datetime) -> Self:
        self._require_running("succeed")
        return self._transition(Succeeded(result=result), now)

    def fail(self, reason: str, now: datetime) -> Self:
        self._require_status("fail", JobStatus.QUEUED, JobStatus.RUNNING)
        return self._transition(Failed(reason=reason, stage=self.stage, progress=self.progress), now)

    def cancel(self, now: datetime) -> Self:
        self._require_status("cancel", JobStatus.QUEUED, JobStatus.RUNNING)
        return self._transition(Cancelled(stage=self.stage, progress=self.progress), now)

    def _require_running(self, action: str) -> Running:
        if isinstance(self.state, Running):
            return self.state
        raise self._illegal_transition(action)

    def _require_status(self, action: str, *allowed: JobStatus) -> None:
        if self.status not in allowed:
            raise self._illegal_transition(action)

    def _illegal_transition(self, action: str) -> InvalidJobTransitionError:
        message = f"cannot {action} job {self.job_id} while it is {self.status}"
        if self.is_terminal:
            return JobAlreadyTerminalError(message)
        return InvalidJobTransitionError(message)

    def _transition(self, state: JobState, now: datetime) -> Self:
        if not is_timezone_aware(now):
            message = "transition time must carry an explicit offset"
            raise InvalidTimestampError(message)
        return replace(self, state=state, updated_at=max(now, self.updated_at))
