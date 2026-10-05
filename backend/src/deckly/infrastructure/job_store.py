import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import CTE, Select, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import ReturningDelete, ReturningUpdate

from deckly.application.exceptions import UnreadableJobRequestError
from deckly.application.ports import IdempotencyScope, JobStore, JobTransition, StoredJob
from deckly.domain.deck import GenerationResult
from deckly.domain.exceptions import InvalidGenerationRequestError
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import (
    TERMINAL_STATUSES,
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

logger = logging.getLogger(__name__)

INSERT_ATTEMPTS = 2


class CorruptStoredJobError(Exception):
    pass


class JobStoreTimeoutError(TimeoutError):
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
        message = f"the saved request of job {row.job_id} no longer validates: {error}"
        raise UnreadableJobRequestError(message) from error


def scoped_row(scope: IdempotencyScope) -> Select[tuple[GenerationJobRow]]:
    return select(GenerationJobRow).where(
        GenerationJobRow.client_id == scope.client_id,
        GenerationJobRow.idempotency_key == scope.idempotency_key,
    )


def restore_stored(row: GenerationJobRow) -> StoredJob:
    return StoredJob(job=restore_job(row), request=restore_request(row))


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
        for _ in range(INSERT_ATTEMPTS):
            async with self._session_factory.begin() as session:
                if await session.scalar(statement) is not None:
                    return StoredJob(job=job, request=request)
                existing = await session.scalar(scoped_row(scope))
            if existing is not None:
                return restore_stored(existing)
        message = f"idempotency conflict for {scope} but no stored job was found"
        raise CorruptStoredJobError(message)

    async def find(self, scope: IdempotencyScope) -> StoredJob | None:
        async with self._session_factory() as session:
            row = await session.scalar(scoped_row(scope))
        return None if row is None else restore_stored(row)

    async def get(self, job_id: UUID) -> GenerationJob | None:
        async with self._session_factory() as session:
            row = await session.get(GenerationJobRow, job_id)
        return None if row is None else restore_job(row)

    async def get_stored(self, job_id: UUID) -> StoredJob | None:
        async with self._session_factory() as session:
            row = await session.get(GenerationJobRow, job_id)
        return None if row is None else restore_stored(row)

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


def evaluated_once(batch: Select[tuple[UUID]]) -> CTE:
    return batch.cte("batch").prefix_with("MATERIALIZED")


def release_expired_keys_statement(created_before: datetime, limit: int) -> ReturningUpdate[tuple[UUID]]:
    expired = (
        select(GenerationJobRow.job_id)
        .where(GenerationJobRow.idempotency_key.is_not(None), GenerationJobRow.created_at < created_before)
        .order_by(GenerationJobRow.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    batch = evaluated_once(expired)
    return (
        update(GenerationJobRow)
        .where(GenerationJobRow.job_id.in_(select(batch.c.job_id)))
        .values(idempotency_key=None)
        .returning(GenerationJobRow.job_id)
    )


def purge_finished_statement(finished_before: datetime, limit: int) -> ReturningDelete[tuple[UUID]]:
    expired = (
        select(GenerationJobRow.job_id)
        .where(GenerationJobRow.status.in_(TERMINAL_STATUSES), GenerationJobRow.updated_at < finished_before)
        .order_by(GenerationJobRow.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    batch = evaluated_once(expired)
    return (
        delete(GenerationJobRow)
        .where(GenerationJobRow.job_id.in_(select(batch.c.job_id)))
        .returning(GenerationJobRow.job_id)
    )


class PostgresJobHousekeeping:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def stale(self, status: JobStatus, updated_before: datetime, limit: int) -> tuple[UUID, ...]:
        statement = (
            select(GenerationJobRow.job_id)
            .where(GenerationJobRow.status == status, GenerationJobRow.updated_at < updated_before)
            .order_by(GenerationJobRow.updated_at)
            .limit(limit)
        )
        async with self._session_factory() as session:
            return tuple(await session.scalars(statement))

    async def release_idempotency_keys(self, created_before: datetime, limit: int) -> int:
        async with self._session_factory.begin() as session:
            return len((await session.scalars(release_expired_keys_statement(created_before, limit))).all())

    async def purge_finished(self, finished_before: datetime, limit: int) -> int:
        async with self._session_factory.begin() as session:
            return len((await session.scalars(purge_finished_statement(finished_before, limit))).all())


class BoundedJobStore:
    def __init__(self, store: JobStore, *, timeout_seconds: float) -> None:
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._abandoned: set[asyncio.Task[object]] = set()

    async def add(self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope) -> StoredJob:
        return await self._bounded(self._store.add(job, request, scope))

    async def find(self, scope: IdempotencyScope) -> StoredJob | None:
        return await self._bounded(self._store.find(scope))

    async def get(self, job_id: UUID) -> GenerationJob | None:
        return await self._bounded(self._store.get(job_id))

    async def get_stored(self, job_id: UUID) -> StoredJob | None:
        return await self._bounded(self._store.get_stored(job_id))

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        return await self._bounded(self._store.update(job_id, transition))

    async def _bounded[T](self, call: Coroutine[object, object, T]) -> T:
        task = asyncio.create_task(call)
        try:
            done, _ = await asyncio.wait({task}, timeout=self._timeout_seconds)
        except asyncio.CancelledError:
            self._abandon(task)
            raise
        if not done:
            self._abandon(task)
            logger.warning("job_store_unresponsive", extra={"timeout_seconds": self._timeout_seconds})
            message = f"the job store did not answer within {self._timeout_seconds} seconds"
            raise JobStoreTimeoutError(message)
        return task.result()

    def _abandon(self, task: asyncio.Task[object]) -> None:
        task.cancel()
        self._abandoned.add(task)
        task.add_done_callback(self._settle)

    def _settle(self, task: asyncio.Task[object]) -> None:
        self._abandoned.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("abandoned_job_store_call_failed", extra={"error": type(error).__name__})
