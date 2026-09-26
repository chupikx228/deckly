import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from uuid import UUID

from deckly.application.exceptions import JobStoppedError, NoValidContentError, UpstreamUnavailableError
from deckly.application.generations import Clock
from deckly.application.ports import (
    CardGenerator,
    JobStore,
    JobTransition,
    MediaFetcher,
    SourceParser,
    SourceRetriever,
    StoredJob,
)
from deckly.domain.deck import GenerationResult
from deckly.domain.exceptions import JobAlreadyTerminalError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import STAGE_ORDER, Failed, FailureCode, GenerationJob, JobStage, JobStatus

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
class RunGeneration:
    store: JobStore
    retriever: SourceRetriever
    parser: SourceParser
    generator: CardGenerator
    media: MediaFetcher
    clock: Clock

    async def __call__(self, job_id: UUID) -> None:
        stored = await self.store.get_stored(job_id)
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
        await self._enter(job_id, JobStage.RETRIEVING_SOURCES)
        pages = await self.retriever.retrieve(request)
        await self._enter(job_id, JobStage.PARSING_SOURCES)
        material = await self.parser.parse(request, pages)
        await self._enter(job_id, JobStage.GENERATING_CARDS)
        result = require_notes(await self.generator.generate(job_id, request, material))
        if request.include_images:
            await self._enter(job_id, JobStage.FETCHING_MEDIA)
            result = require_notes(replace(result, notes=await self.media.fetch(request, result.notes)))
        await self._enter(job_id, JobStage.FINALIZING)
        now = self.clock()
        succeeded = await self._update(job_id, lambda job: job.succeed(result, now))
        logger.info("generation_succeeded", extra={**log_context(succeeded), "notes": len(result.notes)})

    async def _enter(self, job_id: UUID, stage: JobStage) -> None:
        now = self.clock()
        progress = stage_progress(stage)
        entered = await self._update(job_id, lambda job: job.advance(stage, progress, now))
        logger.info("generation_stage_entered", extra=log_context(entered))

    async def _record_interruption(self, job_id: UUID) -> None:
        try:
            interrupted = await self._fail(job_id, FailureCode.GENERATION_FAILED)
        except JobStoppedError:
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
