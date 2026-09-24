from datetime import timedelta

import pytest

from deckly.domain.job import JobStatus
from tests.fakes import QUOTA_LIMIT, Harness, generation_request, job_id, scope

pytestmark = pytest.mark.anyio


async def test_new_request_is_stored_queued_and_enqueued_once() -> None:
    harness = Harness()

    created = await harness.create(generation_request(), scope())

    assert created.job.status is JobStatus.QUEUED
    assert created.job.created_at == harness.now
    assert harness.store.jobs == {created.job.job_id: created.job}
    assert harness.queue.enqueued == [created.job.job_id]
    assert created.quota.limit == QUOTA_LIMIT


async def test_same_key_and_client_returns_the_original_job_without_a_second_one() -> None:
    harness = Harness()
    first = await harness.create(generation_request(), scope(client=1, key=7))
    harness.now += timedelta(seconds=30)

    replay = await harness.create(generation_request("Something else"), scope(client=1, key=7))

    assert replay.job == first.job
    assert list(harness.store.jobs) == [first.job.job_id]
    assert harness.store.requests[first.job.job_id] == generation_request()


async def test_same_key_from_a_different_client_is_a_different_job() -> None:
    harness = Harness()

    first = await harness.create(generation_request(), scope(client=1, key=7))
    second = await harness.create(generation_request(), scope(client=2, key=7))

    assert first.job.job_id != second.job.job_id
    assert len(harness.store.jobs) == 2


async def test_different_key_from_the_same_client_is_a_different_job() -> None:
    harness = Harness()

    first = await harness.create(generation_request(), scope(client=1, key=1))
    second = await harness.create(generation_request(), scope(client=1, key=2))

    assert first.job.job_id != second.job.job_id


async def test_replay_of_a_job_that_left_the_queue_is_not_enqueued_again() -> None:
    harness = Harness()
    first = await harness.create(generation_request(), scope())
    harness.store.replace(first.job.start(harness.now).cancel(harness.now))

    replay = await harness.create(generation_request(), scope())

    assert replay.job.status is JobStatus.CANCELLED
    assert harness.queue.enqueued == [first.job.job_id]


async def test_replay_after_a_failed_enqueue_enqueues_the_same_job() -> None:
    harness = Harness()
    harness.queue.unavailable = True
    with pytest.raises(ConnectionError):
        await harness.create(generation_request(), scope())
    harness.queue.unavailable = False

    replay = await harness.create(generation_request(), scope())

    assert replay.job.job_id == job_id(1)
    assert harness.queue.enqueued == [job_id(1)]
    assert list(harness.store.jobs) == [job_id(1)]


async def test_quota_is_reported_for_a_replay_too() -> None:
    harness = Harness()
    await harness.create(generation_request(), scope())

    replay = await harness.create(generation_request(), scope())

    assert replay.quota.remaining == QUOTA_LIMIT
    assert replay.quota.resets_at > harness.now
