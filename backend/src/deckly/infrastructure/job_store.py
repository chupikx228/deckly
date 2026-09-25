from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import IdempotencyScope, StoredJob
from deckly.domain.exceptions import InvalidGenerationRequestError
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import (
    Cancelled,
    Failed,
    FailureCode,
    GenerationJob,
    JobStage,
    JobState,
    JobStatus,
    Progress,
    Queued,
    Running,
    Succeeded,
)
from deckly.domain.notes.note_type import NoteType
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
    failure_reason = state.code if isinstance(state, Failed) else None
    return StoredState(state.status, state.stage, state.progress.value, failure_reason)


def restore_state(stored: StoredState) -> JobState:
    try:
        status = JobStatus(stored.status)
        stage = None if stored.stage is None else JobStage(stored.stage)
        failure_code = None if stored.failure_reason is None else FailureCode(stored.failure_reason)
    except ValueError as error:
        raise CorruptStoredJobError(str(error)) from error
    progress = Progress(stored.progress)
    match status:
        case JobStatus.QUEUED if stage is None:
            return Queued()
        case JobStatus.RUNNING if stage is not None:
            return Running(stage=stage, progress=progress)
        case JobStatus.FAILED if failure_code is not None:
            return Failed(code=failure_code, stage=stage, progress=progress)
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


def restore_request(row: GenerationJobRow) -> GenerationRequest:
    try:
        return GenerationRequest(
            topic=row.topic,
            language=row.language,
            card_count=row.card_count,
            difficulty=Difficulty(row.difficulty),
            note_types=tuple(NoteType(note_type) for note_type in row.note_types),
            include_images=row.include_images,
            instructions=row.instructions,
        )
    except (ValueError, InvalidGenerationRequestError) as error:
        raise CorruptStoredJobError(str(error)) from error


class PostgresJobStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope) -> StoredJob:
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
                return StoredJob(job=job, request=request)
            existing = await session.scalar(
                select(GenerationJobRow).where(
                    GenerationJobRow.client_id == scope.client_id,
                    GenerationJobRow.idempotency_key == scope.idempotency_key,
                )
            )
        if existing is None:
            message = f"idempotency conflict for {scope} but no stored job was found"
            raise CorruptStoredJobError(message)
        return StoredJob(job=restore_job(existing), request=restore_request(existing))

    async def get(self, job_id: UUID) -> GenerationJob | None:
        async with self._session_factory() as session:
            row = await session.get(GenerationJobRow, job_id)
        return None if row is None else restore_job(row)
