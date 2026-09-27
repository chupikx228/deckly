import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest

from deckly.application.ports import IdempotencyScope, JobTransition, StoredJob
from deckly.domain.exceptions import JobAlreadyTerminalError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob
from deckly.infrastructure.job_store import BoundedJobStore, JobStoreTimeoutError
from tests.domain.builders import T0
from tests.fakes import InMemoryJobStore, generation_request, job_id, scope

pytestmark = pytest.mark.anyio

JOB_STORE_LOGGER = "deckly.infrastructure.job_store"
TIMEOUT_SECONDS = 0.1
SAFETY_NET_SECONDS = 5

type StoreCall = Callable[[BoundedJobStore], Awaitable[object]]


class CleanupFailedError(Exception):
    pass


class StallingJobStore(InMemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.release.set()
        self.cleanup_released = asyncio.Event()
        self.cleanup_released.set()
        self.cleanup_failure: Exception | None = None
        self.cancellations = 0
        self.tasks: list[asyncio.Task[object]] = []

    async def add(self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope) -> StoredJob:
        await self._answer()
        return await super().add(job, request, scope)

    async def find(self, scope: IdempotencyScope) -> StoredJob | None:
        await self._answer()
        return await super().find(scope)

    async def get(self, job_id: UUID) -> GenerationJob | None:
        await self._answer()
        return await super().get(job_id)

    async def get_stored(self, job_id: UUID) -> StoredJob | None:
        await self._answer()
        return await super().get_stored(job_id)

    async def update(self, job_id: UUID, transition: JobTransition) -> GenerationJob | None:
        await self._answer()
        return await super().update(job_id, transition)

    async def _answer(self) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.tasks.append(task)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            await self.cleanup_released.wait()
            if self.cleanup_failure is not None:
                raise self.cleanup_failure from None
            raise


def stalled() -> StallingJobStore:
    store = StallingJobStore()
    store.release.clear()
    return store


def bounded(store: StallingJobStore) -> BoundedJobStore:
    return BoundedJobStore(store, timeout_seconds=TIMEOUT_SECONDS)


def queued(number: int = 1) -> GenerationJob:
    return GenerationJob.queue(job_id(number), T0)


async def within_safety_net[T](call: Awaitable[T]) -> tuple[T, float]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(SAFETY_NET_SECONDS):
        result = await call
    return result, loop.time() - started


async def test_answering_store_passes_every_call_through() -> None:
    store = BoundedJobStore(StallingJobStore(), timeout_seconds=TIMEOUT_SECONDS)
    job = queued()

    added = await store.add(job, generation_request(), scope())
    cancelled = await store.update(job.job_id, lambda stored: stored.cancel(T0))

    assert added == StoredJob(job=job, request=generation_request())
    assert cancelled is not None
    assert await store.get(job.job_id) == cancelled
    assert await store.get_stored(job.job_id) == StoredJob(job=cancelled, request=generation_request())
    assert await store.find(scope()) == StoredJob(job=cancelled, request=generation_request())
    assert await store.find(scope(key=2)) is None
    assert await store.get(job_id(2)) is None


async def test_failure_inside_the_store_reaches_the_caller_unchanged() -> None:
    store = BoundedJobStore(StallingJobStore(), timeout_seconds=TIMEOUT_SECONDS)
    job = queued()
    await store.add(job, generation_request(), scope())
    await store.update(job.job_id, lambda stored: stored.cancel(T0))

    with pytest.raises(JobAlreadyTerminalError):
        await store.update(job.job_id, lambda stored: stored.cancel(T0))


async def test_unanswered_call_times_out_within_its_bound_and_is_cancelled_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = stalled()

    with caplog.at_level(logging.WARNING, logger=JOB_STORE_LOGGER), pytest.raises(JobStoreTimeoutError):
        await within_safety_net(bounded(inner).get(job_id(1)))
    await asyncio.sleep(0)

    assert inner.cancellations == 1
    assert [record.getMessage() for record in caplog.records] == ["job_store_unresponsive"]


@pytest.mark.parametrize(
    "call",
    [
        lambda store: store.add(queued(), generation_request(), scope()),
        lambda store: store.find(scope()),
        lambda store: store.get(job_id(1)),
        lambda store: store.get_stored(job_id(1)),
        lambda store: store.update(job_id(1), lambda stored: stored),
    ],
    ids=["add", "find", "get", "get_stored", "update"],
)
async def test_every_call_is_bounded_even_when_its_cleanup_never_finishes(
    call: StoreCall, caplog: pytest.LogCaptureFixture
) -> None:
    inner = stalled()
    inner.cleanup_released.clear()
    store = bounded(inner)

    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(JobStoreTimeoutError):
            async with asyncio.timeout(SAFETY_NET_SECONDS):
                await call(store)
        elapsed = loop.time() - started
        await asyncio.sleep(0)

        assert TIMEOUT_SECONDS <= elapsed < SAFETY_NET_SECONDS
        [task] = inner.tasks
        assert (inner.cancellations, task.cancelling(), task.done()) == (1, 1, False)

        inner.cleanup_released.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert task.cancelled()
    assert [record.getMessage() for record in caplog.records] == ["job_store_unresponsive"]


async def test_cancelled_caller_cancels_the_call_once_and_does_not_wait_for_its_cleanup() -> None:
    inner = stalled()
    inner.cleanup_released.clear()
    caller = asyncio.create_task(bounded(inner).get(job_id(1)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await within_safety_net(caller)
    await asyncio.sleep(0)

    [task] = inner.tasks
    assert (inner.cancellations, task.cancelling(), task.done()) == (1, 1, False)

    inner.cleanup_released.set()
    await asyncio.sleep(0)

    assert task.cancelled()


async def test_abandoned_call_that_fails_while_cleaning_up_is_logged_not_left_unretrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = stalled()
    inner.cleanup_released.clear()
    inner.cleanup_failure = CleanupFailedError()

    with caplog.at_level(logging.WARNING, logger=JOB_STORE_LOGGER), pytest.raises(JobStoreTimeoutError):
        await within_safety_net(bounded(inner).get(job_id(1)))
    inner.cleanup_released.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    failures = [
        record for record in caplog.records if record.getMessage() == "abandoned_job_store_call_failed"
    ]
    [failure] = failures
    assert failure.__dict__.get("error") == CleanupFailedError.__name__
    [task] = inner.tasks
    assert isinstance(task.exception(), CleanupFailedError)


async def test_next_call_after_a_timeout_runs_again() -> None:
    inner = stalled()
    store = bounded(inner)

    with pytest.raises(JobStoreTimeoutError):
        await within_safety_net(store.get(job_id(1)))
    inner.release.set()

    assert await store.get(job_id(1)) is None
    assert len(inner.tasks) == 2
