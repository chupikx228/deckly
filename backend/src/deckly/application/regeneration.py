import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.generations import Clock
from deckly.application.ports import NoteRegenerator, RegenerationLimiter, SourceParser, SourceRetriever
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.notes.note import Note
from deckly.domain.regeneration import RegenerationRequest

logger = logging.getLogger(__name__)

SINGLE_NOTE = 1
TIMED_OUT_RETRY_AFTER_SECONDS = 5

type RequestIdFactory = Callable[[], UUID]


def retrieval_request(request: RegenerationRequest) -> GenerationRequest:
    return GenerationRequest(
        topic=request.topic,
        language=request.language,
        card_count=SINGLE_NOTE,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=(request.note_type,),
        include_images=False,
        instructions=None,
    )


@dataclass(frozen=True, slots=True)
class RegenerateNote:
    limiter: RegenerationLimiter
    retriever: SourceRetriever
    parser: SourceParser
    regenerator: NoteRegenerator
    clock: Clock
    new_request_id: RequestIdFactory
    timeout_seconds: float

    async def __call__(self, request: RegenerationRequest, client_id: UUID) -> Note:
        request_id = self.new_request_id()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._regenerate(request_id, request, client_id)
        except TimeoutError as error:
            logger.warning(
                "note_regeneration_timed_out",
                extra={"request_id": str(request_id), "timeout_seconds": self.timeout_seconds},
            )
            raise UpstreamUnavailableError(TIMED_OUT_RETRY_AFTER_SECONDS) from error

    async def _regenerate(self, request_id: UUID, request: RegenerationRequest, client_id: UUID) -> Note:
        await self.limiter.acquire(client_id, self.clock())
        search = retrieval_request(request)
        pages = await self.retriever.retrieve(request_id, search)
        material = await self.parser.parse(request_id, search, pages)
        return await self.regenerator.regenerate(request_id, request, material)
