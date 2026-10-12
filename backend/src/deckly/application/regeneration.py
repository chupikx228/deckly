import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from uuid import UUID

from deckly.application.correlation import REQUEST_ID, correlated
from deckly.application.exceptions import NoValidContentError, UpstreamUnavailableError
from deckly.application.generations import Clock
from deckly.application.ports import (
    NoteRegenerator,
    RegenerationLimiter,
    RegenerationModerator,
    SourceParser,
    SourceRetriever,
)
from deckly.domain.exceptions import TopicRejectedError
from deckly.domain.notes.note import Note
from deckly.domain.regeneration import RegenerationRequest

logger = logging.getLogger(__name__)

TIMED_OUT_RETRY_AFTER_SECONDS = 5

type RequestIdFactory = Callable[[], UUID]


async def alongside[T](work: Coroutine[object, object, T], check: Coroutine[object, object, None]) -> T:
    try:
        async with asyncio.TaskGroup() as group:
            result = group.create_task(work)
            group.create_task(check)
    except BaseExceptionGroup as failures:
        raise failures.exceptions[0] from None
    return result.result()


@dataclass(frozen=True, slots=True)
class RegenerateNote:
    limiter: RegenerationLimiter
    retriever: SourceRetriever
    parser: SourceParser
    regenerator: NoteRegenerator
    moderator: RegenerationModerator
    clock: Clock
    new_request_id: RequestIdFactory
    timeout_seconds: float

    async def __call__(self, request: RegenerationRequest, client_id: UUID) -> Note:
        request_id = self.new_request_id()
        try:
            with correlated(REQUEST_ID, request_id):
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
        query = request.source_query()
        pages = await alongside(
            self.retriever.retrieve(request_id, query), self._screen_request(request_id, request)
        )
        material = await self.parser.parse(request_id, query, pages)
        note = await self.regenerator.regenerate(request_id, request, material)
        if not await self.moderator.allows_note(request_id, query, note):
            logger.warning("regenerated_note_blocked", extra={"request_id": str(request_id)})
            message = "the regenerated note violates the content policy"
            raise NoValidContentError(message)
        return note

    async def _screen_request(self, request_id: UUID, request: RegenerationRequest) -> None:
        if not await self.moderator.allows_request(request):
            logger.info("regeneration_request_rejected", extra={"request_id": str(request_id)})
            message = "the topic or the rejected note violates the content policy"
            raise TopicRejectedError(message)
