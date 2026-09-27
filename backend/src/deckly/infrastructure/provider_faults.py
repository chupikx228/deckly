import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import NoteRegenerator, RetrievedPage, SourceMaterial, SourceRetriever
from deckly.domain.generation import GenerationRequest
from deckly.domain.notes.note import Note
from deckly.domain.regeneration import RegenerationRequest
from deckly.infrastructure.llm.client import LlmError
from deckly.infrastructure.search.client import SearchError

PROVIDER_FAULT_RETRY_AFTER_SECONDS = 5

logger = logging.getLogger(__name__)


@contextmanager
def provider_faults_as_upstream(request_id: UUID) -> Iterator[None]:
    try:
        yield
    except (LlmError, SearchError) as error:
        logger.exception(
            "provider_fault",
            extra={"request_id": str(request_id), "error": type(error).__name__},
        )
        raise UpstreamUnavailableError(PROVIDER_FAULT_RETRY_AFTER_SECONDS) from error


@dataclass(frozen=True, slots=True)
class UpstreamFaultRetriever:
    inner: SourceRetriever

    async def retrieve(self, job_id: UUID, request: GenerationRequest) -> tuple[RetrievedPage, ...]:
        with provider_faults_as_upstream(job_id):
            return await self.inner.retrieve(job_id, request)


@dataclass(frozen=True, slots=True)
class UpstreamFaultRegenerator:
    inner: NoteRegenerator

    async def regenerate(
        self, request_id: UUID, request: RegenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> Note:
        with provider_faults_as_upstream(request_id):
            return await self.inner.regenerate(request_id, request, material)
