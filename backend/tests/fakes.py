import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from deckly.application.generations import CancelGeneration, CreateGeneration, GetGeneration
from deckly.application.pipeline import RunGeneration
from deckly.application.ports import (
    IdempotencyScope,
    JobTransition,
    Quota,
    RetrievedPage,
    SourceMaterial,
    StoredJob,
)
from deckly.domain.deck import GenerationResult
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import GenerationJob, JobStage
from deckly.domain.media import Media, MediaKind
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.llm.client import LlmPrompt, LlmReply, LlmStop
from deckly.infrastructure.quota import QUOTA_WINDOW
from tests.domain.builders import SOURCES, T0, basic_note, result_with

QUOTA_LIMIT = 20
IMAGE_NUMBER_OFFSET = 1000
MODEL_THINKING_SECONDS = 5
PAGES = (RetrievedPage(source=SOURCES[0], content="<p>A red triangle warns of danger ahead.</p>"),)
MATERIAL = (SourceMaterial(source=SOURCES[0], text="A red triangle warns of danger ahead."),)
GENERATED = result_with(basic_note(1), basic_note(2), basic_note(3))

type Hook = Callable[[], Awaitable[object]]
type LlmOutcome = LlmReply | Exception | Callable[[], Awaitable[LlmReply]]


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


class FixedQuota:
    async def current(self, client_id: UUID, now: datetime) -> Quota:
        del client_id
        return Quota(limit=QUOTA_LIMIT, remaining=QUOTA_LIMIT, resets_at=now + QUOTA_WINDOW)


class FakeProviders:
    def __init__(self) -> None:
        self.result = GENERATED
        self.enrich: Callable[[tuple[Note, ...]], tuple[Note, ...]] = with_images
        self.calls: list[JobStage] = []
        self.received: dict[JobStage, object] = {}
        self.generated_for: list[UUID] = []
        self.failures: dict[JobStage, Exception] = {}
        self.during: dict[JobStage, Hook] = {}

    async def retrieve(self, request: GenerationRequest) -> tuple[RetrievedPage, ...]:
        await self._reach(JobStage.RETRIEVING_SOURCES, request)
        return PAGES

    async def parse(
        self, request: GenerationRequest, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]:
        del request
        await self._reach(JobStage.PARSING_SOURCES, pages)
        return MATERIAL

    async def generate(
        self, job_id: UUID, request: GenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> GenerationResult:
        del request
        self.generated_for.append(job_id)
        await self._reach(JobStage.GENERATING_CARDS, material)
        return self.result

    async def fetch(self, request: GenerationRequest, notes: tuple[Note, ...]) -> tuple[Note, ...]:
        del request
        await self._reach(JobStage.FETCHING_MEDIA, notes)
        return self.enrich(notes)

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
        self.now = T0
        ids = sequential_job_ids()
        self.create = CreateGeneration(
            store=self.store,
            queue=self.queue,
            quota=FixedQuota(),
            clock=lambda: self.now,
            new_job_id=lambda: next(ids),
        )
        self.get = GetGeneration(store=self.store)
        self.cancel = CancelGeneration(store=self.store, queue=self.queue, clock=lambda: self.now)
        self.run = RunGeneration(
            store=self.store,
            retriever=self.providers,
            parser=self.providers,
            generator=self.providers,
            media=self.providers,
            clock=lambda: self.now,
        )
