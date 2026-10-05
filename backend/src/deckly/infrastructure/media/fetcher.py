import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import ImageQuery, NoteMedia
from deckly.infrastructure.media.client import ImageCandidate, ImageSearch, ImageSearchClient, MediaError
from deckly.infrastructure.media.licensing import licensed_image
from deckly.infrastructure.resilience import CallSize, ResilientCaller

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MediaLimits:
    max_images: int
    candidates_per_query: int
    thumbnail_width: int
    max_concurrency: int
    deadline_seconds: float


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    query: ImageQuery
    candidates: tuple[ImageCandidate, ...] = ()
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class SearchRound:
    outcomes: tuple[SearchOutcome, ...]
    deadline_reached: bool


def one_query_per_note(queries: Iterable[ImageQuery], limit: int) -> tuple[ImageQuery, ...]:
    by_note: dict[UUID, ImageQuery] = {}
    for query in queries:
        by_note.setdefault(query.client_id, query)
    return tuple(by_note.values())[:limit]


def finished_outcome(task: asyncio.Task[SearchOutcome]) -> SearchOutcome | None:
    if not task.done() or task.cancelled() or task.exception() is not None:
        return None
    return task.result()


@dataclass(frozen=True, slots=True)
class CommonsMediaFetcher:
    client: ImageSearchClient
    caller: ResilientCaller
    new_id: Callable[[], UUID]
    limits: MediaLimits

    async def fetch(self, _job_id: UUID, queries: tuple[ImageQuery, ...]) -> tuple[NoteMedia, ...]:
        searched = await self._search_all(one_query_per_note(queries, self.limits.max_images))
        attachments = self._choose(searched.outcomes)
        failures = Counter(outcome.failure for outcome in searched.outcomes if outcome.failure is not None)
        logger.log(
            logging.WARNING if failures or searched.deadline_reached else logging.INFO,
            "media_fetched",
            extra={
                "image_queries": len(queries),
                "searched": len(searched.outcomes),
                "attached": len(attachments),
                "failed_searches": dict(failures),
                "deadline_reached": searched.deadline_reached,
            },
        )
        return attachments

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _search_all(self, queries: tuple[ImageQuery, ...]) -> SearchRound:
        gate = asyncio.Semaphore(self.limits.max_concurrency)
        tasks: list[asyncio.Task[SearchOutcome]] = []
        deadline_reached = False
        try:
            async with asyncio.timeout(self.limits.deadline_seconds), asyncio.TaskGroup() as group:
                tasks.extend(group.create_task(self._search(query, gate)) for query in queries)
        except TimeoutError:
            deadline_reached = True
        outcomes = (finished_outcome(task) for task in tasks)
        return SearchRound(
            outcomes=tuple(outcome for outcome in outcomes if outcome is not None),
            deadline_reached=deadline_reached,
        )

    async def _search(self, query: ImageQuery, gate: asyncio.Semaphore) -> SearchOutcome:
        search = ImageSearch(
            text=query.text,
            max_candidates=self.limits.candidates_per_query,
            thumbnail_width=self.limits.thumbnail_width,
        )
        async with gate:
            try:
                candidates = await self.caller.call(lambda: self.client.search(search), size=CallSize.TYPICAL)
            except (UpstreamUnavailableError, MediaError) as error:
                return SearchOutcome(query=query, failure=type(error).__name__)
        return SearchOutcome(query=query, candidates=candidates)

    def _choose(self, outcomes: Iterable[SearchOutcome]) -> tuple[NoteMedia, ...]:
        used: set[str] = set()
        attachments: list[NoteMedia] = []
        for outcome in outcomes:
            for candidate in outcome.candidates:
                if candidate.file_title in used:
                    continue
                media = licensed_image(candidate, self.new_id)
                if media is None:
                    continue
                used.add(candidate.file_title)
                attachments.append(NoteMedia(client_id=outcome.query.client_id, media=media))
                break
        return tuple(attachments)
