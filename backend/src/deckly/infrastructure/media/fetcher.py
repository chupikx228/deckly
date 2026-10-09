import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import ImageQuery, NoteMedia
from deckly.domain.deck import GenerationResult
from deckly.domain.notes.note import Note
from deckly.infrastructure.media.client import ImageCandidate, ImageSearch, ImageSearchClient, MediaError
from deckly.infrastructure.media.judging import CandidateJudge, Ranking, Shortlist, ShortlistedImage
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
    judged_candidates_per_note: int


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


def chosen(shortlists: Iterable[Shortlist], rankings: Iterable[Ranking]) -> tuple[NoteMedia, ...]:
    used: set[str] = set()
    attachments: list[NoteMedia] = []
    for shortlist, ranking in zip(shortlists, rankings, strict=True):
        image = next((image for image in ranking if image.candidate.file_title not in used), None)
        if image is None:
            continue
        used.add(image.candidate.file_title)
        attachments.append(NoteMedia(client_id=shortlist.note.client_id, media=image.media))
    return tuple(attachments)


def finished_outcome(task: asyncio.Task[SearchOutcome]) -> SearchOutcome | None:
    if not task.done() or task.cancelled() or task.exception() is not None:
        return None
    return task.result()


@dataclass(frozen=True, slots=True)
class CommonsMediaFetcher:
    client: ImageSearchClient
    caller: ResilientCaller
    judge: CandidateJudge
    new_id: Callable[[], UUID]
    limits: MediaLimits

    async def fetch(
        self, _job_id: UUID, result: GenerationResult, queries: tuple[ImageQuery, ...]
    ) -> tuple[NoteMedia, ...]:
        notes = {note.client_id: note for note in result.notes}
        illustrable = (query for query in queries if query.client_id in notes)
        searched = await self._search_all(one_query_per_note(illustrable, self.limits.max_images))
        shortlists = self._shortlist(searched.outcomes, notes)
        rankings = await self.judge.rank(result.deck, shortlists) if shortlists else ()
        attachments = chosen(shortlists, rankings)
        failures = Counter(outcome.failure for outcome in searched.outcomes if outcome.failure is not None)
        logger.log(
            logging.WARNING if failures or searched.deadline_reached else logging.INFO,
            "media_fetched",
            extra={
                "image_queries": len(queries),
                "searched": len(searched.outcomes),
                "shortlisted": len(shortlists),
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

    def _shortlist(
        self, outcomes: Iterable[SearchOutcome], notes: Mapping[UUID, Note]
    ) -> tuple[Shortlist, ...]:
        shortlists: list[Shortlist] = []
        for outcome in outcomes:
            images = self._licensed(outcome.candidates)
            if images:
                note = notes[outcome.query.client_id]
                shortlists.append(Shortlist(note=note, picture=outcome.query.text, images=images))
        return tuple(shortlists)

    def _licensed(self, candidates: Iterable[ImageCandidate]) -> tuple[ShortlistedImage, ...]:
        seen: set[str] = set()
        images: list[ShortlistedImage] = []
        for candidate in candidates:
            if len(images) == self.limits.judged_candidates_per_note:
                break
            if candidate.file_title in seen:
                continue
            media = licensed_image(candidate, self.new_id)
            if media is not None:
                seen.add(candidate.file_title)
                images.append(ShortlistedImage(candidate=candidate, media=media))
        return tuple(images)
