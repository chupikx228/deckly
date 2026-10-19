import asyncio
import itertools
import time
from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from arq.constants import default_queue_name
from arq.utils import timestamp_ms
from arq.worker import Worker

from deckly.application.pipeline import RunGeneration
from deckly.application.ports import IdempotencyScope, QueueLane
from deckly.config import TRANSCRIPTION_WORKER_MAX_JOBS
from deckly.domain.document import Document, DocumentFormat
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob, JobStage, JobStatus
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.queue import TRANSCRIPTION_QUEUE_NAME
from deckly.worker.settings import RUN_GENERATION_KEY, TranscriptionWorkerSettings, WorkerSettings
from tests.domain.builders import T0
from tests.fakes import (
    DOCUMENT_CLIENT_ID,
    UPLOADED_DOCUMENT,
    FakeDocuments,
    FakeProviders,
    InMemoryJobStore,
    InMemoryResultCache,
    document_request,
    fresh_telemetry,
    generation_request,
)
from tests.integration.conftest import Cleanup, job_queue

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

RECORDING_JOBS = 3
TRANSCRIPTION_SECONDS = 1.0
GENERATION_SLOTS = RECORDING_JOBS
POLL_DELAY_SECONDS = 0.05
NO_WAIT_SECONDS = 0.5
NEXT_MILLISECOND_SLEEP_SECONDS = 0.002


@dataclass
class Timeline:
    started: float = field(default_factory=time.monotonic)
    transcriptions: list[tuple[float, float]] = field(default_factory=list)
    in_flight: int = 0
    most_in_flight: int = 0
    topic_started: float | None = None

    def now(self) -> float:
        return time.monotonic() - self.started

    @property
    def first_transcription_finished(self) -> float:
        return min(end for _, end in self.transcriptions)


@dataclass
class Workload:
    store: InMemoryJobStore
    run: RunGeneration
    timeline: Timeline
    recordings: list[UUID]
    topic: UUID | None = None


def recording(number: int) -> Document:
    return replace(
        UPLOADED_DOCUMENT,
        document_id=UUID(f"00000000-0000-4000-8000-{number:012x}"),
        filename=f"lecture-{number}.m4a",
        document_format=DocumentFormat.M4A,
        duration_seconds=600.0,
    )


def workload() -> Workload:
    timeline = Timeline()
    providers = FakeProviders()
    documents = FakeDocuments()
    store = InMemoryJobStore()

    async def transcribe() -> None:
        start = timeline.now()
        timeline.in_flight += 1
        timeline.most_in_flight = max(timeline.most_in_flight, timeline.in_flight)
        try:
            await asyncio.sleep(TRANSCRIPTION_SECONDS)
        finally:
            timeline.in_flight -= 1
            timeline.transcriptions.append((start, timeline.now()))

    async def research_topic() -> None:
        timeline.topic_started = timeline.now()

    documents.during["parse"] = transcribe
    providers.during[JobStage.RETRIEVING_SOURCES] = research_topic
    for number in range(1, RECORDING_JOBS + 1):
        documents.keep(recording(number))
    run = RunGeneration(
        store=store,
        cache=InMemoryResultCache(),
        retriever=providers,
        parser=providers,
        documents=documents.sourcing(),
        generator=providers,
        moderator=providers,
        media=providers,
        clock=utc_now,
        telemetry=fresh_telemetry(),
    )
    return Workload(store=store, run=run, timeline=timeline, recordings=[])


@dataclass(frozen=True, slots=True)
class Queues:
    pool: ArqRedis
    cleanup: Cleanup


async def wait_for_next_millisecond() -> None:
    enqueued_at = timestamp_ms()
    await asyncio.sleep(NEXT_MILLISECOND_SLEEP_SECONDS)
    assert timestamp_ms() > enqueued_at


async def admit(work: Workload, queues: Queues, request: GenerationRequest, lane: QueueLane) -> UUID:
    job = GenerationJob.queue(uuid4(), T0)
    queues.cleanup.job_ids.add(job.job_id)
    await work.store.add(
        job, request, IdempotencyScope(client_id=DOCUMENT_CLIENT_ID, idempotency_key=uuid4())
    )
    await job_queue(queues.pool).enqueue(job.job_id, lane)
    await wait_for_next_millisecond()
    return job.job_id


async def admit_recordings_then_a_topic(work: Workload, queues: Queues, *, recording_lane: QueueLane) -> None:
    for number in range(1, RECORDING_JOBS + 1):
        request = document_request(recording(number).document_id)
        work.recordings.append(await admit(work, queues, request, recording_lane))
    work.topic = await admit(work, queues, generation_request(), QueueLane.GENERATION)


def worker(pool: ArqRedis, work: Workload, *, transcription: bool, max_jobs: int) -> Worker:
    settings = TranscriptionWorkerSettings if transcription else WorkerSettings
    return Worker(
        functions=settings.functions,
        redis_pool=pool,
        queue_name=TRANSCRIPTION_QUEUE_NAME if transcription else default_queue_name,
        ctx={RUN_GENERATION_KEY: work.run},
        handle_signals=False,
        burst=True,
        max_jobs=max_jobs,
        poll_delay=POLL_DELAY_SECONDS,
    )


def statuses(work: Workload) -> set[JobStatus]:
    return {work.store.jobs[job_id].status for job_id in [*work.recordings, work.topic] if job_id is not None}


async def test_topic_job_starts_at_once_while_recordings_wait_one_at_a_time_on_their_own_queue(
    queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    work = workload()
    await admit_recordings_then_a_topic(
        work, Queues(queue_pool, cleanup), recording_lane=QueueLane.TRANSCRIPTION
    )

    await asyncio.gather(
        worker(queue_pool, work, transcription=False, max_jobs=GENERATION_SLOTS).async_run(),
        worker(queue_pool, work, transcription=True, max_jobs=TRANSCRIPTION_WORKER_MAX_JOBS).async_run(),
    )

    timeline = work.timeline
    assert statuses(work) == {JobStatus.SUCCEEDED}
    assert timeline.topic_started is not None
    assert timeline.topic_started < NO_WAIT_SECONDS
    assert timeline.topic_started < timeline.first_transcription_finished
    assert timeline.most_in_flight == 1
    ordered = sorted(timeline.transcriptions)
    assert all(earlier[1] <= later[0] for earlier, later in itertools.pairwise(ordered))


async def test_the_same_jobs_on_one_shared_queue_make_the_topic_wait_behind_concurrent_transcriptions(
    queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    work = workload()
    await admit_recordings_then_a_topic(
        work, Queues(queue_pool, cleanup), recording_lane=QueueLane.GENERATION
    )

    await worker(queue_pool, work, transcription=False, max_jobs=GENERATION_SLOTS).async_run()

    timeline = work.timeline
    assert statuses(work) == {JobStatus.SUCCEEDED}
    assert timeline.topic_started is not None
    assert timeline.topic_started >= timeline.first_transcription_finished
    assert timeline.most_in_flight == RECORDING_JOBS
