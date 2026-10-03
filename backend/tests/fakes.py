import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import httpx2

from deckly.application.exceptions import RateLimitedError
from deckly.application.generations import CancelGeneration, CreateGeneration, GetGeneration
from deckly.application.pipeline import RunGeneration
from deckly.application.ports import (
    GeneratedCards,
    IdempotencyScope,
    ImageQuery,
    JobTransition,
    NoteMedia,
    Quota,
    Requester,
    RetrievedPage,
    SourceMaterial,
    StoredJob,
)
from deckly.application.regeneration import RegenerateNote
from deckly.domain.deck import GenerationResult
from deckly.domain.generation import Difficulty, GenerationFingerprint, GenerationRequest
from deckly.domain.job import GenerationJob, JobStage
from deckly.domain.media import Media, MediaKind
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.regeneration import RegenerationRequest, RejectionReason
from deckly.infrastructure.llm.client import LlmPrompt, LlmReply, LlmStop
from deckly.infrastructure.media.client import ImageCandidate, ImageSearch, MediaEndpoint
from deckly.infrastructure.media.commons_client import CommonsImageSearchClient
from deckly.infrastructure.media.fetcher import CommonsMediaFetcher, MediaLimits
from deckly.infrastructure.quota import QUOTA_WINDOW
from deckly.infrastructure.resilience import CircuitBreaker, ResilientCaller, RetryPolicy, RetryRuntime
from deckly.infrastructure.search.client import SearchEndpoint, SearchHit, SearchQuery
from deckly.infrastructure.search.parser import CleaningSourceParser
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from tests.domain.builders import SOURCES, T0, basic_note, result_with

QUOTA_LIMIT = 20
ADDRESS_QUOTA_LIMIT = 100
QUOTA_RETRY_AFTER_SECONDS = 3600
ADDRESS = "203.0.113.7"
IMAGE_NUMBER_OFFSET = 1000
MODEL_THINKING_SECONDS = 5
RESET_SECONDS = 30
SEARCH_ENDPOINT = SearchEndpoint(base_url="https://api.tavily.test", api_key="tvly-test", timeout_seconds=5)
SEARCH_POLICY = RetryPolicy(
    max_attempts=2,
    attempt_timeout_seconds=5,
    deadline_seconds=20,
    base_delay_seconds=1,
    max_delay_seconds=2,
)
SEARCH_MAX_RESULTS = 6
MEDIA_ENDPOINT = MediaEndpoint(
    base_url="https://commons.wikimedia.test",
    user_agent="DecklyTest/0.1 (tests@example.com)",
    timeout_seconds=5,
)
MEDIA_POLICY = RetryPolicy(
    max_attempts=2,
    attempt_timeout_seconds=5,
    deadline_seconds=20,
    base_delay_seconds=1,
    max_delay_seconds=2,
)
MEDIA_LIMITS = MediaLimits(
    max_images=20, candidates_per_query=10, thumbnail_width=960, max_concurrency=4, deadline_seconds=20
)
SOURCE_MAX_CHARACTERS = 2000
PAGES = (RetrievedPage(source=SOURCES[0], content="<p>A red triangle warns of danger ahead.</p>"),)
MATERIAL = (SourceMaterial(source=SOURCES[0], text="A red triangle warns of danger ahead."),)
GENERATED = result_with(basic_note(1), basic_note(2), basic_note(3))

type Hook = Callable[[], Awaitable[object]]
type TopicOutcome = bool | Exception | Callable[[], Awaitable[bool]]
type LlmOutcome = LlmReply | Exception | Callable[[], Awaitable[LlmReply]]
type SearchOutcome = tuple[SearchHit, ...] | Exception | Callable[[], Awaitable[tuple[SearchHit, ...]]]
type ImageOutcome = (
    tuple[ImageCandidate, ...] | Exception | Callable[[], Awaitable[tuple[ImageCandidate, ...]]]
)


def model_reply(document: object, stop: LlmStop = LlmStop.COMPLETE) -> LlmReply:
    return LlmReply(text=json.dumps(document), stop=stop)


async def hang_forever() -> LlmReply:
    await asyncio.Event().wait()
    message = "a hanging model call never returns"
    raise AssertionError(message)


class FakeLlmClient:
    def __init__(self, *outcomes: LlmOutcome) -> None:
        self.outcomes = list(outcomes)
        self.prompts: list[LlmPrompt] = []
        self.closed = False

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        self.prompts.append(prompt)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, LlmReply):
            return outcome
        if isinstance(outcome, Exception):
            raise outcome
        return await outcome()

    async def aclose(self) -> None:
        self.closed = True


class FakeSearchClient:
    def __init__(self, *outcomes: SearchOutcome) -> None:
        self.outcomes = list(outcomes)
        self.queries: list[SearchQuery] = []
        self.closed = False

    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        self.queries.append(query)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, tuple):
            return outcome
        if isinstance(outcome, Exception):
            raise outcome
        return await outcome()

    async def aclose(self) -> None:
        self.closed = True


class FakeImageSearchClient:
    def __init__(self, *outcomes: ImageOutcome) -> None:
        self.outcomes = list(outcomes)
        self.searches: list[ImageSearch] = []
        self.closed = False

    async def search(self, search: ImageSearch) -> tuple[ImageCandidate, ...]:
        self.searches.append(search)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, tuple):
            return outcome
        if isinstance(outcome, Exception):
            raise outcome
        return await outcome()

    async def aclose(self) -> None:
        self.closed = True


class ManualTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.fraction = 1.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def jitter(self) -> float:
        return self.fraction

    def breaker(self, failure_threshold: int = 5) -> CircuitBreaker:
        return CircuitBreaker(
            failure_threshold=failure_threshold, reset_seconds=RESET_SECONDS, clock=self.clock
        )

    def runtime(self) -> RetryRuntime:
        return RetryRuntime(clock=self.clock, sleep=self.sleep, jitter=self.jitter)


def tavily_result(title: str, url: str, raw_content: str | None) -> dict[str, object]:
    return {"title": title, "url": url, "content": "", "raw_content": raw_content, "score": 0.5}


def web_sources(
    handler: Callable[[httpx2.Request], httpx2.Response], time: ManualTime, clock: Callable[[], datetime]
) -> tuple[WebSourceRetriever, CleaningSourceParser]:
    retriever = WebSourceRetriever(
        client=TavilySearchClient(SEARCH_ENDPOINT, httpx2.MockTransport(handler)),
        caller=ResilientCaller(SEARCH_POLICY, time.breaker(), time.runtime()),
        clock=clock,
        max_results=SEARCH_MAX_RESULTS,
    )
    return retriever, CleaningSourceParser(max_characters=SOURCE_MAX_CHARACTERS)


COMMONS_METADATA_KEYS = {
    "license_code": "License",
    "attribution_required": "AttributionRequired",
    "restrictions": "Restrictions",
    "description": "ImageDescription",
}
LICENSED_METADATA: dict[str, str | None] = {
    "license_code": "cc0",
    "attribution_required": "false",
    "restrictions": "",
    "description": "A red octagonal stop sign",
}


def commons_page(
    index: int,
    title: str = "File:Stop sign.svg",
    *,
    info: dict[str, object] | None = None,
    **metadata: str | None,
) -> dict[str, object]:
    values = {COMMONS_METADATA_KEYS[key]: value for key, value in (LICENSED_METADATA | metadata).items()}
    name = title.removeprefix("File:").replace(" ", "_")
    image_info: dict[str, object] = {
        "mime": "image/svg+xml",
        "thumbwidth": 960,
        "thumbheight": 960,
        "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{name}/960px-{name}.png",
        "extmetadata": {key: {"value": value} for key, value in values.items() if value is not None},
        **(info or {}),
    }
    return {"pageid": index, "ns": 6, "title": title, "index": index, "imageinfo": [image_info]}


def commons_results(*pages: dict[str, object]) -> dict[str, object]:
    if not pages:
        return {"batchcomplete": True}
    return {"batchcomplete": True, "query": {"pages": list(pages)}}


def commons_fetcher(
    handler: Callable[[httpx2.Request], httpx2.Response],
    time: ManualTime,
    limits: MediaLimits = MEDIA_LIMITS,
    breaker: CircuitBreaker | None = None,
) -> CommonsMediaFetcher:
    return CommonsMediaFetcher(
        client=CommonsImageSearchClient(MEDIA_ENDPOINT, httpx2.MockTransport(handler)),
        caller=ResilientCaller(MEDIA_POLICY, breaker or time.breaker(), time.runtime()),
        new_id=sequential_job_ids().__next__,
        limits=limits,
    )


class SlowModel:
    def __init__(self, answer: LlmReply) -> None:
        self.answer = answer
        self.reached = asyncio.Event()
        self.endings: list[str] = []

    async def think(self) -> LlmReply:
        self.reached.set()
        try:
            await asyncio.sleep(MODEL_THINKING_SECONDS)
        except asyncio.CancelledError:
            self.endings.append("aborted")
            raise
        self.endings.append("answered")
        return self.answer


def job_id(number: int) -> UUID:
    return UUID(int=number, version=4)


def sequential_job_ids() -> Iterator[UUID]:
    number = 1
    while True:
        yield job_id(number)
        number += 1


def generation_request(topic: str = "Road signs") -> GenerationRequest:
    return GenerationRequest(
        topic=topic,
        language="ru",
        card_count=40,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=(NoteType.BASIC,),
        include_images=False,
        instructions=None,
    )


def scope(client: int = 1, key: int = 1) -> IdempotencyScope:
    return IdempotencyScope(client_id=UUID(int=client, version=4), idempotency_key=UUID(int=key, version=4))


def image(number: int) -> Media:
    return Media(
        media_id=UUID(int=IMAGE_NUMBER_OFFSET + number, version=4),
        kind=MediaKind.IMAGE,
        url=f"https://example.com/images/{number}.png",
        license="CC0-1.0",
        alt=f"Illustration {number}",
    )


def with_images(notes: tuple[Note, ...]) -> tuple[Note, ...]:
    return tuple(
        replace(note, media=(*note.media, image(number))) for number, note in enumerate(notes, start=1)
    )


def image_query(note: Note) -> ImageQuery:
    return ImageQuery(client_id=note.client_id, text=f"picture for note {note.client_id.int}")


def queries_for_every_note(result: GenerationResult) -> tuple[ImageQuery, ...]:
    return tuple(image_query(note) for note in result.notes)


def images_for(queries: tuple[ImageQuery, ...]) -> tuple[NoteMedia, ...]:
    return tuple(
        NoteMedia(client_id=query.client_id, media=image(number))
        for number, query in enumerate(queries, start=1)
    )


class InMemoryJobStore:
    def __init__(self) -> None:
        self.jobs: dict[UUID, GenerationJob] = {}
        self.requests: dict[UUID, GenerationRequest] = {}
        self.job_ids_by_scope: dict[IdempotencyScope, UUID] = {}
        self.history: list[GenerationJob] = []
        self.update_calls = 0

    async def add(self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope) -> StoredJob:
        existing = self.job_ids_by_scope.get(scope)
        if existing is not None:
            return StoredJob(job=self.jobs[existing], request=self.requests[existing])
        self.job_ids_by_scope[scope] = job.job_id
        self.jobs[job.job_id] = job
        self.requests[job.job_id] = request
        return StoredJob(job=job, request=request)

    async def find(self, scope: IdempotencyScope) -> StoredJob | None:
        existing = self.job_ids_by_scope.get(scope)
        return (
            None if existing is None else StoredJob(job=self.jobs[existing], request=self.requests[existing])
        )

    async def get(self, job_id: UUID) -> GenerationJob | None:
        return self.jobs.get(job_id)

    async def get_stored(self, job_id: UUID) -> StoredJob | None:
        job = self.jobs.get(job_id)
        return None if job is None else StoredJob(job=job, request=self.requests[job_id])

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        self.update_calls += 1
        job = self.jobs.get(job_id)
        if job is None:
            return None
        self.jobs[job_id] = transition(job)
        self.history.append(self.jobs[job_id])
        return self.jobs[job_id]

    def replace(self, job: GenerationJob) -> None:
        self.jobs[job.job_id] = job


class InMemoryResultCache:
    def __init__(self) -> None:
        self.entries: dict[GenerationFingerprint, GenerationResult] = {}
        self.reads: list[GenerationRequest] = []
        self.writes: list[tuple[GenerationRequest, GenerationResult]] = []
        self.read_failure: Exception | None = None
        self.write_failure: Exception | None = None

    async def get(self, request: GenerationRequest) -> GenerationResult | None:
        self.reads.append(request)
        if self.read_failure is not None:
            raise self.read_failure
        return self.entries.get(request.fingerprint())

    async def put(self, request: GenerationRequest, result: GenerationResult) -> None:
        self.writes.append((request, result))
        if self.write_failure is not None:
            raise self.write_failure
        self.entries[request.fingerprint()] = result


class RecordingJobQueue:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []
        self.aborted: list[UUID] = []
        self.running: dict[UUID, asyncio.Task[None]] = {}
        self.unavailable = False

    async def enqueue(self, job_id: UUID) -> None:
        if self.unavailable:
            message = "queue unavailable"
            raise ConnectionError(message)
        self.enqueued.append(job_id)

    async def abort(self, job_id: UUID) -> None:
        self.aborted.append(job_id)
        task = self.running.get(job_id)
        if task is not None:
            task.cancel()


class InMemoryQuota:
    def __init__(self, per_client: int = QUOTA_LIMIT, per_address: int = ADDRESS_QUOTA_LIMIT) -> None:
        self.per_client = per_client
        self.per_address = per_address
        self.used_by_client: Counter[UUID] = Counter()
        self.used_by_address: Counter[str] = Counter()
        self.released: list[Requester] = []
        self.failure: Exception | None = None

    async def current(self, client_id: UUID, now: datetime) -> Quota:
        self._fail_if_unavailable()
        return self._quota(client_id, now)

    async def reserve(self, requester: Requester, now: datetime) -> Quota:
        self._fail_if_unavailable()
        if (
            self.used_by_client[requester.client_id] >= self.per_client
            or self.used_by_address[requester.address] >= self.per_address
        ):
            raise RateLimitedError(QUOTA_RETRY_AFTER_SECONDS)
        self.used_by_client[requester.client_id] += 1
        self.used_by_address[requester.address] += 1
        return self._quota(requester.client_id, now)

    async def release(self, requester: Requester, now: datetime) -> None:
        del now
        self.released.append(requester)
        self.used_by_client[requester.client_id] = max(0, self.used_by_client[requester.client_id] - 1)
        self.used_by_address[requester.address] = max(0, self.used_by_address[requester.address] - 1)

    def _quota(self, client_id: UUID, now: datetime) -> Quota:
        remaining = max(0, self.per_client - self.used_by_client[client_id])
        return Quota(limit=self.per_client, remaining=remaining, resets_at=now + QUOTA_WINDOW)

    def _fail_if_unavailable(self) -> None:
        if self.failure is not None:
            raise self.failure


class FakeTopicModerator:
    def __init__(self) -> None:
        self.outcome: TopicOutcome = True
        self.screened: list[GenerationRequest] = []

    async def allows(self, request: GenerationRequest) -> bool:
        self.screened.append(request)
        if isinstance(self.outcome, bool):
            return self.outcome
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return await self.outcome()


class FakeProviders:
    def __init__(self) -> None:
        self.result = GENERATED
        self.unsafe_notes: set[UUID] = set()
        self.screened: list[GenerationResult] = []
        self.screen_failure: Exception | None = None
        self.image_queries: tuple[ImageQuery, ...] | None = None
        self.attach: Callable[[tuple[ImageQuery, ...]], tuple[NoteMedia, ...]] = images_for
        self.calls: list[JobStage] = []
        self.received: dict[JobStage, object] = {}
        self.retrieved_for: list[UUID] = []
        self.parsed_for: list[UUID] = []
        self.generated_for: list[UUID] = []
        self.fetched_for: list[UUID] = []
        self.failures: dict[JobStage, Exception] = {}
        self.during: dict[JobStage, Hook] = {}

    async def retrieve(self, job_id: UUID, request: GenerationRequest) -> tuple[RetrievedPage, ...]:
        self.retrieved_for.append(job_id)
        await self._reach(JobStage.RETRIEVING_SOURCES, request)
        return PAGES

    async def parse(
        self, job_id: UUID, request: GenerationRequest, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]:
        del request
        self.parsed_for.append(job_id)
        await self._reach(JobStage.PARSING_SOURCES, pages)
        return MATERIAL

    async def generate(
        self, job_id: UUID, request: GenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> GeneratedCards:
        del request
        self.generated_for.append(job_id)
        await self._reach(JobStage.GENERATING_CARDS, material)
        queries = queries_for_every_note(self.result) if self.image_queries is None else self.image_queries
        return GeneratedCards(result=self.result, image_queries=queries)

    async def screen(
        self, job_id: UUID, request: GenerationRequest, result: GenerationResult
    ) -> GenerationResult:
        del job_id, request
        self.screened.append(result)
        if self.screen_failure is not None:
            raise self.screen_failure
        return replace(
            result, notes=tuple(note for note in result.notes if note.client_id not in self.unsafe_notes)
        )

    async def fetch(self, job_id: UUID, queries: tuple[ImageQuery, ...]) -> tuple[NoteMedia, ...]:
        self.fetched_for.append(job_id)
        await self._reach(JobStage.FETCHING_MEDIA, queries)
        return self.attach(queries)

    async def _reach(self, stage: JobStage, received: object) -> None:
        self.calls.append(stage)
        self.received[stage] = received
        hook = self.during.get(stage)
        if hook is not None:
            await hook()
        failure = self.failures.get(stage)
        if failure is not None:
            raise failure


class Harness:
    def __init__(self, store: InMemoryJobStore | None = None, queue: RecordingJobQueue | None = None) -> None:
        self.store = InMemoryJobStore() if store is None else store
        self.queue = RecordingJobQueue() if queue is None else queue
        self.providers = FakeProviders()
        self.cache = InMemoryResultCache()
        self.quota = InMemoryQuota()
        self.moderator = FakeTopicModerator()
        self.now = T0
        ids = sequential_job_ids()
        self.create = CreateGeneration(
            store=self.store,
            queue=self.queue,
            quota=self.quota,
            moderator=self.moderator,
            clock=lambda: self.now,
            new_job_id=lambda: next(ids),
        )
        self.get = GetGeneration(store=self.store)
        self.cancel = CancelGeneration(store=self.store, queue=self.queue, clock=lambda: self.now)
        self.run = RunGeneration(
            store=self.store,
            cache=self.cache,
            retriever=self.providers,
            parser=self.providers,
            generator=self.providers,
            moderator=self.providers,
            media=self.providers,
            clock=lambda: self.now,
        )


class RegenerationStep(StrEnum):
    LIMIT = "limit"
    SEARCH = "search"
    PARSE = "parse"
    GENERATE = "generate"


def regeneration_request(
    note_type: NoteType = NoteType.BASIC, reason: RejectionReason = RejectionReason.TOO_EASY
) -> RegenerationRequest:
    return RegenerationRequest(
        topic="Road signs",
        language="ru",
        note_type=note_type,
        rejected_fields={"front": "front 9", "back": "back"},
        reason=reason,
    )


class FakeRegeneration:
    def __init__(self) -> None:
        self.note = basic_note(1)
        self.steps: list[RegenerationStep] = []
        self.acquired: list[tuple[UUID, datetime]] = []
        self.searched: list[tuple[UUID, GenerationRequest]] = []
        self.parsed: list[tuple[UUID, tuple[RetrievedPage, ...]]] = []
        self.regenerated: list[tuple[UUID, RegenerationRequest, tuple[SourceMaterial, ...]]] = []
        self.cancelled: list[RegenerationStep] = []
        self.failures: dict[RegenerationStep, Exception] = {}
        self.during: dict[RegenerationStep, Hook] = {}

    async def acquire(self, client_id: UUID, now: datetime) -> None:
        self.acquired.append((client_id, now))
        await self._reach(RegenerationStep.LIMIT)

    async def retrieve(self, job_id: UUID, request: GenerationRequest) -> tuple[RetrievedPage, ...]:
        self.searched.append((job_id, request))
        await self._reach(RegenerationStep.SEARCH)
        return PAGES

    async def parse(
        self, job_id: UUID, request: GenerationRequest, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]:
        del request
        self.parsed.append((job_id, pages))
        await self._reach(RegenerationStep.PARSE)
        return MATERIAL

    async def regenerate(
        self, request_id: UUID, request: RegenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> Note:
        self.regenerated.append((request_id, request, material))
        await self._reach(RegenerationStep.GENERATE)
        return self.note

    async def _reach(self, step: RegenerationStep) -> None:
        self.steps.append(step)
        hook = self.during.get(step)
        if hook is not None:
            try:
                await hook()
            except asyncio.CancelledError:
                self.cancelled.append(step)
                raise
        failure = self.failures.get(step)
        if failure is not None:
            raise failure


class RegenerationHarness:
    def __init__(self, timeout_seconds: float = 10) -> None:
        self.providers = FakeRegeneration()
        self.now = T0
        ids = sequential_job_ids()
        self.regenerate = RegenerateNote(
            limiter=self.providers,
            retriever=self.providers,
            parser=self.providers,
            regenerator=self.providers,
            clock=lambda: self.now,
            new_request_id=lambda: next(ids),
            timeout_seconds=timeout_seconds,
        )
