import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from uuid import UUID

from deckly.application.exceptions import (
    JobStoppedError,
    NoValidContentError,
    UnreadableJobRequestError,
    UpstreamUnavailableError,
)
from deckly.application.generations import Clock
from deckly.application.ports import (
    CardGenerator,
    ContentModerator,
    ImageQuery,
    JobStore,
    JobTransition,
    MediaFetcher,
    NoteMedia,
    ResultCache,
    SourceParser,
    SourceRetriever,
    StoredJob,
)
from deckly.domain.deck import GenerationResult
from deckly.domain.exceptions import JobAlreadyTerminalError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import STAGE_ORDER, Failed, FailureCode, GenerationJob, JobStage, JobStatus
from deckly.domain.media import Media
from deckly.domain.notes.note import Note

logger = logging.getLogger(__name__)

FAILURE_CODES: Mapping[type[Exception], FailureCode] = {
    UpstreamUnavailableError: FailureCode.PROVIDER_UNAVAILABLE,
    NoValidContentError: FailureCode.NO_VALID_CONTENT,
}
EXPECTED_FAILURES = frozenset({FailureCode.PROVIDER_UNAVAILABLE, FailureCode.NO_VALID_CONTENT})


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


def failure_level(error: Exception) -> int:
    return logging.WARNING if isinstance(error, UpstreamUnavailableError) else logging.ERROR


def log_context(job: GenerationJob) -> dict[str, object]:
    context: dict[str, object] = {
        "job_id": str(job.job_id),
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress.value,
    }
    if isinstance(job.state, Failed):
        context["failure_code"] = job.state.code
    return context


@dataclass(frozen=True, slots=True)
class Illustrated:
    result: GenerationResult
    complete: bool


@dataclass(frozen=True, slots=True)
class RunGeneration:
    store: JobStore
    cache: ResultCache
    retriever: SourceRetriever
    parser: SourceParser
    generator: CardGenerator
    moderator: ContentModerator
    media: MediaFetcher
    clock: Clock

    async def __call__(self, job_id: UUID) -> None:
        try:
            stored = await self.store.get_stored(job_id)
        except UnreadableJobRequestError:
            await self._reject_unreadable(job_id)
            return
        if stored is None or stored.job.is_terminal:
            logger.info("generation_skipped", extra={"job_id": str(job_id)})
            return
        try:
            await self._run(stored)
        except JobStoppedError:
            logger.info("generation_stopped", extra={"job_id": str(job_id)})

    async def _run(self, stored: StoredJob) -> None:
        job_id = stored.job.job_id
        if stored.job.status is JobStatus.RUNNING:
            abandoned = await self._fail(job_id, FailureCode.GENERATION_FAILED)
            logger.warning("generation_abandoned", extra=log_context(abandoned))
            return
        now = self.clock()
        await self._update(job_id, lambda job: job.start(now))
        try:
            await self._generate(job_id, stored.request)
        except JobStoppedError:
            raise
        except asyncio.CancelledError:
            await self._record_interruption(job_id)
            raise
        except Exception as error:
            code = failure_code_for(error)
            failed = await self._fail(job_id, code)
            level = logging.WARNING if code in EXPECTED_FAILURES else logging.ERROR
            logger.log(level, "generation_failed", exc_info=error, extra=log_context(failed))

    async def _generate(self, job_id: UUID, request: GenerationRequest) -> None:
        cached = await self._recall(job_id, request)
        if cached is not None:
            await self._succeed(job_id, cached, cache_hit=True)
            return
        await self._enter(job_id, JobStage.RETRIEVING_SOURCES)
        pages = await self.retriever.retrieve(job_id, request)
        await self._enter(job_id, JobStage.PARSING_SOURCES)
        material = await self.parser.parse(job_id, request, pages)
        await self._enter(job_id, JobStage.GENERATING_CARDS)
        cards = await self.generator.generate(job_id, request, material)
        result = require_notes(await self.moderator.screen(job_id, request, require_notes(cards.result)))
        cacheable = True
        if request.include_images:
            await self._enter(job_id, JobStage.FETCHING_MEDIA)
            illustrated = await self._illustrate(job_id, result, queries_for(result, cards.image_queries))
            result, cacheable = illustrated.result, illustrated.complete
        await self._enter(job_id, JobStage.FINALIZING)
        await self._succeed(job_id, result, cache_hit=False)
        if cacheable:
            await self._remember(job_id, request, result)

    async def _succeed(self, job_id: UUID, result: GenerationResult, *, cache_hit: bool) -> None:
        now = self.clock()
        succeeded = await self._update(job_id, lambda job: job.succeed(result, now))
        logger.info(
            "generation_succeeded",
            extra={**log_context(succeeded), "notes": len(result.notes), "cache_hit": cache_hit},
        )

    async def _recall(self, job_id: UUID, request: GenerationRequest) -> GenerationResult | None:
        try:
            return await self.cache.get(request)
        except Exception as error:
            logger.log(
                failure_level(error),
                "generation_cache_read_failed",
                exc_info=error,
                extra={"job_id": str(job_id)},
            )
            return None

    async def _remember(self, job_id: UUID, request: GenerationRequest, result: GenerationResult) -> None:
        try:
            await self.cache.put(request, result)
        except Exception as error:
            logger.log(
                failure_level(error),
                "generation_cache_write_failed",
                exc_info=error,
                extra={"job_id": str(job_id)},
            )

    async def _illustrate(
        self, job_id: UUID, result: GenerationResult, queries: tuple[ImageQuery, ...]
    ) -> Illustrated:
        try:
            illustrated = attach_media(result, await self.media.fetch(job_id, queries))
        except Exception as error:
            logger.log(
                failure_level(error),
                "media_skipped",
                exc_info=error,
                extra={"job_id": str(job_id), "image_queries": len(queries)},
            )
            return Illustrated(result=result, complete=False)
        illustrated_notes = sum(
            1 for before, after in zip(result.notes, illustrated.notes, strict=True) if after is not before
        )
        logger.info(
            "media_attached",
            extra={
                "job_id": str(job_id),
                "image_queries": len(queries),
                "illustrated_notes": illustrated_notes,
            },
        )
        return Illustrated(result=illustrated, complete=True)

    async def _enter(self, job_id: UUID, stage: JobStage) -> None:
        now = self.clock()
        progress = stage_progress(stage)
        entered = await self._update(job_id, lambda job: job.advance(stage, progress, now))
        logger.info("generation_stage_entered", extra=log_context(entered))

    async def _reject_unreadable(self, job_id: UUID) -> None:
        try:
            rejected = await self._fail(job_id, FailureCode.GENERATION_FAILED)
        except JobStoppedError:
            logger.info("generation_stopped", extra={"job_id": str(job_id)})
            return
        logger.error("generation_request_unreadable", extra=log_context(rejected))

    async def _record_interruption(self, job_id: UUID) -> None:
        try:
            interrupted = await self._fail(job_id, FailureCode.GENERATION_FAILED)
        except JobStoppedError:
            logger.info("generation_stopped", extra={"job_id": str(job_id)})
            return
        except Exception:
            logger.exception("generation_interruption_not_recorded", extra={"job_id": str(job_id)})
            return
        logger.warning("generation_interrupted", extra=log_context(interrupted))

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
