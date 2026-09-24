from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import IdempotencyScope
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import (
    Cancelled,
    Failed,
    GenerationJob,
    JobStage,
    JobState,
    JobStatus,
    Progress,
    Queued,
    Running,
    Succeeded,
)
from deckly.infrastructure.tables import GenerationJobRow


class UnstorableJobStateError(Exception):
    pass


class CorruptStoredJobError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StoredState:
    status: str
    stage: str | None
    progress: float
    failure_reason: str | None


def store_state(state: JobState) -> StoredState:
    if isinstance(state, Succeeded):
        message = "persisting a generation result is not supported yet"
        raise UnstorableJobStateError(message)
    failure_reason = state.reason if isinstance(state, Failed) else None
    return StoredState(state.status, state.stage, state.progress.value, failure_reason)


def restore_state(stored: StoredState) -> JobState:
    try:
        status = JobStatus(stored.status)
        stage = None if stored.stage is None else JobStage(stored.stage)
    except ValueError as error:
        raise CorruptStoredJobError(str(error)) from error
    progress = Progress(stored.progress)
    match status:
        case JobStatus.QUEUED if stage is None:
            return Queued()
        case JobStatus.RUNNING if stage is not None:
            return Running(stage=stage, progress=progress)
        case JobStatus.FAILED if stored.failure_reason is not None:
            return Failed(reason=stored.failure_reason, stage=stage, progress=progress)
        case JobStatus.CANCELLED:
            return Cancelled(stage=stage, progress=progress)
    message = f"stored job state {stored} cannot be restored"
    raise CorruptStoredJobError(message)


def restore_job(row: GenerationJobRow) -> GenerationJob:
    stored = StoredState(row.status, row.stage, row.progress, row.failure_reason)
    return GenerationJob(
        job_id=row.job_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        state=restore_state(stored),
    )


class PostgresJobStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope
    ) -> GenerationJob:
        stored = store_state(job.state)
        statement = (
            insert(GenerationJobRow)
            .values(
                job_id=job.job_id,
                client_id=scope.client_id,
                idempotency_key=scope.idempotency_key,
                topic=request.topic,
                language=request.language,
                card_count=request.card_count,
                difficulty=request.difficulty,
                note_types=list(request.note_types),
                include_images=request.include_images,
                instructions=request.instructions,
                status=stored.status,
                stage=stored.stage,
                progress=stored.progress,
                failure_reason=stored.failure_reason,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            .on_conflict_do_nothing(
                index_elements=[GenerationJobRow.client_id, GenerationJobRow.idempotency_key]
            )
            .returning(GenerationJobRow.job_id)
        )
        async with self._session_factory.begin() as session:
            if await session.scalar(statement) is not None:
                return job
            existing = await session.scalar(
                select(GenerationJobRow).where(
                    GenerationJobRow.client_id == scope.client_id,
                    GenerationJobRow.idempotency_key == scope.idempotency_key,
                )
            )
        if existing is None:
            message = f"idempotency conflict for {scope} but no stored job was found"
            raise CorruptStoredJobError(message)
        return restore_job(existing)
