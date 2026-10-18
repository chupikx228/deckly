import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from deckly.application.correlation import JOB_ID, correlated
from deckly.application.exceptions import (
    DocumentHasNoTextError,
    DocumentRejectedError,
    EncryptedDocumentError,
    JobStoppedError,
    NoValidContentError,
    UnreadableDocumentError,
    UnreadableJobRequestError,
    UpstreamUnavailableError,
)
from deckly.application.generations import Clock, elapsed_ms
from deckly.application.ports import (
    CardGenerator,
    ContentModerator,
    DocumentParser,
    DocumentStorage,
    DocumentStore,
    GenerationTelemetry,
    ImageQuery,
    JobOutcome,
    JobRun,
    JobStore,
    JobTransition,
    MaterialModerator,
    MediaFetcher,
    MissingImage,
    MissingImageReason,
    NoteMedia,
    ResultCache,
    SourceMaterial,
    SourceParser,
    SourceRetriever,
    StoredJob,
)
from deckly.domain.deck import GenerationResult
from deckly.domain.document import Document
from deckly.domain.exceptions import DocumentNotFoundError, JobAlreadyTerminalError
from deckly.domain.generation import Fingerprint, GenerationRequest, SourceKind
from deckly.domain.job import STAGE_ORDER, Failed, FailureCode, GenerationJob, JobStage, JobStatus
from deckly.domain.media import Media
from deckly.domain.notes.note import Note

logger = logging.getLogger(__name__)

FAILURE_CODES: Mapping[type[Exception], FailureCode] = {
    UpstreamUnavailableError: FailureCode.PROVIDER_UNAVAILABLE,
    NoValidContentError: FailureCode.NO_VALID_CONTENT,
    UnreadableDocumentError: FailureCode.DOCUMENT_UNREADABLE,
    EncryptedDocumentError: FailureCode.DOCUMENT_ENCRYPTED,
    DocumentHasNoTextError: FailureCode.DOCUMENT_HAS_NO_TEXT,
    DocumentNotFoundError: FailureCode.DOCUMENT_NOT_FOUND,
    DocumentRejectedError: FailureCode.DOCUMENT_REJECTED,
}
EXPECTED_FAILURES = frozenset(
    {
        FailureCode.PROVIDER_UNAVAILABLE,
        FailureCode.NO_VALID_CONTENT,
        FailureCode.DOCUMENT_UNREADABLE,
        FailureCode.DOCUMENT_ENCRYPTED,
        FailureCode.DOCUMENT_HAS_NO_TEXT,
        FailureCode.DOCUMENT_NOT_FOUND,
        FailureCode.DOCUMENT_REJECTED,
    }
)


def failure_code_for(error: Exception) -> FailureCode:
    for cls in type(error).__mro__:
        code = FAILURE_CODES.get(cls)
        if code is not None:
            return code
    return FailureCode.GENERATION_FAILED


def stage_progress(stage: JobStage) -> float:
    return stage.position / len(STAGE_ORDER)


def require_notes(result: GenerationResult) -> GenerationResult:
    if not result.notes:
        message = "every generated note was dropped during validation"
        raise NoValidContentError(message)
    return result


def queries_for(result: GenerationResult, queries: Iterable[ImageQuery]) -> tuple[ImageQuery, ...]:
    client_ids = {note.client_id for note in result.notes}
    return tuple(query for query in queries if query.client_id in client_ids)


def with_media(note: Note, media: Iterable[Media]) -> Note:
    known = {item.media_id for item in note.media}
    added = tuple({item.media_id: item for item in media if item.media_id not in known}.values())
    return replace(note, media=(*note.media, *added)) if added else note


def attach_media(result: GenerationResult, attachments: Iterable[NoteMedia]) -> GenerationResult:
    media_by_note: dict[UUID, list[Media]] = {}
    for attachment in attachments:
        media_by_note.setdefault(attachment.client_id, []).append(attachment.media)
    notes = tuple(with_media(note, media_by_note.get(note.client_id, ())) for note in result.notes)
    return replace(result, notes=notes)


def failed_image_notes(result: GenerationResult, missing: Iterable[MissingImage]) -> int:
    client_ids = {note.client_id for note in result.notes}
    failed = {
        item.client_id
        for item in missing
        if item.reason is MissingImageReason.FAILED and item.client_id in client_ids
    }
    return len(failed)


def failure_level(error: Exception) -> int:
    return logging.WARNING if isinstance(error, UpstreamUnavailableError) else logging.ERROR


def log_context(
    job: GenerationJob, request: GenerationRequest | None = None, kind: SourceKind | None = None
) -> dict[str, object]:
    context: dict[str, object] = {
        "job_id": str(job.job_id),
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress.value,
    }
    if isinstance(job.state, Failed):
        context["failure_code"] = job.state.code
    if kind is not None:
        context["source_kind"] = kind
    if request is not None and request.document_id is not None:
        context["document_id"] = str(request.document_id)
    return context


@dataclass(frozen=True, slots=True)
class Illustrated:
    result: GenerationResult
    complete: bool


@dataclass(frozen=True, slots=True)
class Sourced:
    subject: str
    material: tuple[SourceMaterial, ...]


@dataclass(frozen=True, slots=True)
class JobSource:
    job_id: UUID
    request: GenerationRequest
    document: Document | None

    @property
    def kind(self) -> SourceKind:
        return self.request.source_kind(self.document)

    @property
    def fingerprint(self) -> Fingerprint:
        return self.request.fingerprint(self.document)

    def require_document(self) -> None:
        if self.request.document_id is not None and self.document is None:
            message = f"document {self.request.document_id} is no longer stored"
            raise DocumentNotFoundError(message)


@dataclass(frozen=True, slots=True)
class DocumentSourcing:
    store: DocumentStore
    storage: DocumentStorage
    parser: DocumentParser
    moderator: MaterialModerator


@dataclass(frozen=True, slots=True)
class RunGeneration:
    store: JobStore
    cache: ResultCache
    retriever: SourceRetriever
    parser: SourceParser
    documents: DocumentSourcing
    generator: CardGenerator
    moderator: ContentModerator
    media: MediaFetcher
    clock: Clock
    telemetry: GenerationTelemetry

    async def __call__(self, job_id: UUID) -> None:
        with correlated(JOB_ID, job_id), self.telemetry.run(job_id) as run:
            await self._execute(job_id, run)

    async def _execute(self, job_id: UUID, run: JobRun) -> None:
        try:
            stored = await self.store.get_stored(job_id)
        except UnreadableJobRequestError:
            await self._reject_unreadable(job_id, run)
            return
        if stored is None or stored.job.is_terminal:
            logger.info("generation_skipped", extra={"job_id": str(job_id)})
            run.finish(JobOutcome.SKIPPED)
            return
        try:
            await self._run(stored, run)
        except JobStoppedError:
            logger.info("generation_stopped", extra={"job_id": str(job_id)})
            run.finish(JobOutcome.STOPPED)

    async def _run(self, stored: StoredJob, run: JobRun) -> None:
        job_id, request = stored.job.job_id, stored.request
        if stored.job.status is JobStatus.RUNNING:
            abandoned = await self._fail(job_id, FailureCode.GENERATION_FAILED)
            run.finish(JobOutcome.FAILED, FailureCode.GENERATION_FAILED)
            source = await self._resolve(stored)
            run.identify(source.kind)
            logger.warning("generation_abandoned", extra=log_context(abandoned, request, source.kind))
            return
        now = self.clock()
        started = await self._update(job_id, lambda job: job.start(now))
        kind: SourceKind | None = None
        try:
            source = await self._resolve(stored)
            kind = source.kind
            run.identify(kind)
            logger.info("generation_started", extra=log_context(started, request, kind))
            await self._generate(source, now, run)
        except JobStoppedError:
            raise
        except asyncio.CancelledError:
            await self._record_interruption(job_id, run)
            raise
        except Exception as error:
            code = failure_code_for(error)
            failed = await self._fail(job_id, code)
            level = logging.WARNING if code in EXPECTED_FAILURES else logging.ERROR
            logger.log(
                level,
                "generation_failed",
                exc_info=error,
                extra={
                    **log_context(failed, request, kind),
                    "duration_ms": elapsed_ms(now, failed.updated_at),
                },
            )
            run.finish(JobOutcome.FAILED, code)

    async def _resolve(self, stored: StoredJob) -> JobSource:
        job_id, document_id = stored.job.job_id, stored.request.document_id
        if document_id is None:
            return JobSource(job_id=job_id, request=stored.request, document=None)
        document = await self.documents.store.get(document_id, stored.client_id)
        return JobSource(job_id=job_id, request=stored.request, document=document)

    async def _generate(self, source: JobSource, started: datetime, run: JobRun) -> None:
        job_id = source.job_id
        source.require_document()
        with self.telemetry.stage(JobStage.PLANNING, source.kind):
            cached = await self._recall(source)
            if cached is not None:
                await self._succeed(source, cached, started, cache_hit=True)
                run.finish(JobOutcome.SUCCEEDED)
                return
        sourced = await self._source(source)
        async with self._stage(JobStage.GENERATING_CARDS, source):
            cards = await self.generator.generate(job_id, source.request, sourced.material)
            screened = await self.moderator.screen(job_id, sourced.subject, require_notes(cards.result))
            result = require_notes(screened)
        cacheable = True
        if source.request.include_images:
            async with self._stage(JobStage.FETCHING_MEDIA, source):
                illustrated = await self._illustrate(job_id, result, queries_for(result, cards.image_queries))
            result, cacheable = illustrated.result, illustrated.complete
        async with self._stage(JobStage.FINALIZING, source):
            await self._succeed(source, result, started, cache_hit=False)
            run.finish(JobOutcome.SUCCEEDED)
            if cacheable:
                await self._remember(source, result)

    async def _source(self, source: JobSource) -> Sourced:
        job_id, document = source.job_id, source.document
        if document is None:
            query = source.request.source_query()
            async with self._stage(JobStage.RETRIEVING_SOURCES, source):
                pages = await self.retriever.retrieve(job_id, query)
            async with self._stage(JobStage.PARSING_SOURCES, source):
                material = await self.parser.parse(job_id, query, pages)
            return Sourced(subject=query.topic, material=material)
        async with self._stage(JobStage.RETRIEVING_SOURCES, source):
            content = await self._download(job_id, document)
        async with self._stage(JobStage.PARSING_SOURCES, source):
            material = await self.documents.parser.parse(document, content)
            if not await self.documents.moderator.allows(job_id, document, material):
                message = f"document {document.document_id} violates the content policy"
                raise DocumentRejectedError(message)
        return Sourced(subject=document.filename, material=material)

    async def _download(self, job_id: UUID, document: Document) -> bytes:
        content = await self.documents.storage.get(document.storage_key, document.size_bytes)
        logger.info(
            "document_fetched",
            extra={
                "job_id": str(job_id),
                "document_id": str(document.document_id),
                "content_type": document.document_format,
                "size_bytes": len(content),
            },
        )
        return content

    async def _succeed(
        self, source: JobSource, result: GenerationResult, started: datetime, *, cache_hit: bool
    ) -> None:
        now = self.clock()
        succeeded = await self._update(source.job_id, lambda job: job.succeed(result, now))
        logger.info(
            "generation_succeeded",
            extra={
                **log_context(succeeded, source.request, source.kind),
                "notes": len(result.notes),
                "cache_hit": cache_hit,
                "duration_ms": elapsed_ms(started, succeeded.updated_at),
            },
        )

    async def _recall(self, source: JobSource) -> GenerationResult | None:
        try:
            return await self.cache.get(source.fingerprint)
        except Exception as error:
            logger.log(
                failure_level(error),
                "generation_cache_read_failed",
                exc_info=error,
                extra={"job_id": str(source.job_id)},
            )
            return None

    async def _remember(self, source: JobSource, result: GenerationResult) -> None:
        try:
            await self.cache.put(source.fingerprint, result)
        except Exception as error:
            logger.log(
                failure_level(error),
                "generation_cache_write_failed",
                exc_info=error,
                extra={"job_id": str(source.job_id)},
            )

    async def _illustrate(
        self, job_id: UUID, result: GenerationResult, queries: tuple[ImageQuery, ...]
    ) -> Illustrated:
        try:
            fetched = await self.media.fetch(job_id, result, queries)
            illustrated = attach_media(result, fetched.attachments)
        except Exception as error:
            logger.log(
                failure_level(error),
                "media_skipped",
                exc_info=error,
                extra={"job_id": str(job_id), "image_queries": len(queries)},
            )
            return Illustrated(result=result, complete=False)
        failed_notes = failed_image_notes(result, fetched.missing)
        illustrated_notes = sum(
            1 for before, after in zip(result.notes, illustrated.notes, strict=True) if after is not before
        )
        logger.info(
            "media_attached",
            extra={
                "job_id": str(job_id),
                "image_queries": len(queries),
                "illustrated_notes": illustrated_notes,
                "failed_image_notes": failed_notes,
            },
        )
        return Illustrated(result=illustrated, complete=failed_notes == 0)

    @asynccontextmanager
    async def _stage(self, stage: JobStage, source: JobSource) -> AsyncIterator[None]:
        with self.telemetry.stage(stage, source.kind):
            await self._enter(stage, source)
            yield

    async def _enter(self, stage: JobStage, source: JobSource) -> None:
        now = self.clock()
        progress = stage_progress(stage)
        entered = await self._update(source.job_id, lambda job: job.advance(stage, progress, now))
        logger.info("generation_stage_entered", extra=log_context(entered, source.request, source.kind))

    async def _reject_unreadable(self, job_id: UUID, run: JobRun) -> None:
        try:
            rejected = await self._fail(job_id, FailureCode.GENERATION_FAILED)
        except JobStoppedError:
            logger.info("generation_stopped", extra={"job_id": str(job_id)})
            run.finish(JobOutcome.STOPPED)
            return
        logger.error("generation_request_unreadable", extra=log_context(rejected))
        run.finish(JobOutcome.FAILED, FailureCode.GENERATION_FAILED)

    async def _record_interruption(self, job_id: UUID, run: JobRun) -> None:
        try:
            interrupted = await self._fail(job_id, FailureCode.GENERATION_FAILED)
        except JobStoppedError:
            logger.info("generation_stopped", extra={"job_id": str(job_id)})
            run.finish(JobOutcome.STOPPED)
            return
        except Exception:
            logger.exception("generation_interruption_not_recorded", extra={"job_id": str(job_id)})
            return
        logger.warning("generation_interrupted", extra=log_context(interrupted))
        run.finish(JobOutcome.FAILED, FailureCode.GENERATION_FAILED)

    async def _fail(self, job_id: UUID, code: FailureCode) -> GenerationJob:
        now = self.clock()
        return await self._update(job_id, lambda job: job.fail(code, now))

    async def _update(self, job_id: UUID, transition: JobTransition) -> GenerationJob:
        try:
            job = await self.store.update(job_id, transition)
        except JobAlreadyTerminalError as error:
            message = f"job {job_id} was finished elsewhere, most likely cancelled"
            raise JobStoppedError(message) from error
        if job is None:
            message = f"job {job_id} no longer exists"
            raise JobStoppedError(message)
        return job
