from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import IdempotencyScope, JobTransition, StoredJob
from deckly.domain.deck import GenerationResult
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
from deckly.infrastructure.stored_result import dump_result, load_result
from deckly.infrastructure.tables import GenerationJobRow


class CorruptStoredJobError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StoredState:
    status: str
    stage: str | None
    progress: float
    failure_code: str | None
    result: dict[str, object] | None = field(repr=False)


def store_state(state: JobState) -> StoredState:
    failure_code = state.code if isinstance(state, Failed) else None
    result = dump_result(state.result) if isinstance(state, Succeeded) else None
    return StoredState(state.status, state.stage, state.progress.value, failure_code, result)


def restore_state(stored: StoredState) -> JobState:
    try:
        status = JobStatus(stored.status)
        stage = None if stored.stage is None else JobStage(stored.stage)
        failure_code = None if stored.failure_code is None else FailureCode(stored.failure_code)
    except ValueError as error:
        raise CorruptStoredJobError(str(error)) from error
    progress = Progress(stored.progress)
    match status:
        case JobStatus.QUEUED if stage is None:
            return Queued()
        case JobStatus.RUNNING if stage is not None:
            return Running(stage=stage, progress=progress)
        case JobStatus.SUCCEEDED if stage is None and stored.result is not None:
            return Succeeded(result=restore_result(stored.result))
        case JobStatus.FAILED if failure_code is not None:
            return Failed(code=failure_code, stage=stage, progress=progress)
        case JobStatus.CANCELLED:
            return Cancelled(stage=stage, progress=progress)
    message = f"stored job state {stored} cannot be restored"
    raise CorruptStoredJobError(message)


def restore_result(payload: dict[str, object]) -> GenerationResult:
    try:
        return load_result(payload)
    except ValueError as error:
        raise CorruptStoredJobError(str(error)) from error


def restore_job(row: GenerationJobRow) -> GenerationJob:
    stored = StoredState(row.status, row.stage, row.progress, row.failure_code, row.result)
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
                failure_code=stored.failure_code,
                result=stored.result,
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

    async def get_stored(self, job_id: UUID) -> StoredJob | None:
        async with self._session_factory() as session:
            row = await session.get(GenerationJobRow, job_id)
        return None if row is None else StoredJob(job=restore_job(row), request=restore_request(row))

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        async with self._session_factory.begin() as session:
            row = await session.get(GenerationJobRow, job_id, with_for_update=True)
            if row is None:
                return None
            job = transition(restore_job(row))
            stored = store_state(job.state)
            row.status = stored.status
            row.stage = stored.stage
            row.progress = stored.progress
            row.failure_code = stored.failure_code
            row.result = stored.result
            row.updated_at = job.updated_at
        return job
