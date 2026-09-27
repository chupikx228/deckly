from datetime import timedelta

import pytest

from deckly.application.exceptions import (
    IdempotencyKeyConflictError,
    RateLimitedError,
    UpstreamUnavailableError,
)
from deckly.application.ports import IdempotencyScope, Requester, StoredJob
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import GenerationJob, JobStatus
from tests.domain.builders import T0
from tests.fakes import (
    ADDRESS,
    QUOTA_LIMIT,
    QUOTA_RETRY_AFTER_SECONDS,
    Harness,
    InMemoryJobStore,
    generation_request,
    job_id,
    scope,
)

OTHER_ADDRESS = "198.51.100.23"

pytestmark = pytest.mark.anyio


async def test_new_request_is_stored_queued_and_enqueued_once() -> None:
    harness = Harness()

    created = await harness.create(generation_request(), scope(), ADDRESS)

    assert created.job.status is JobStatus.QUEUED
    assert created.job.created_at == harness.now
    assert harness.store.jobs == {created.job.job_id: created.job}
    assert harness.queue.enqueued == [created.job.job_id]
    assert created.quota.limit == QUOTA_LIMIT


async def test_same_key_and_client_returns_the_original_job_without_a_second_one() -> None:
    harness = Harness()
    first = await harness.create(generation_request(), scope(client=1, key=7), ADDRESS)
    harness.now += timedelta(seconds=30)

    replay = await harness.create(generation_request(), scope(client=1, key=7), ADDRESS)

    assert replay.job == first.job
    assert list(harness.store.jobs) == [first.job.job_id]


async def test_same_key_and_client_with_a_different_request_is_a_conflict() -> None:
    harness = Harness()
    first = await harness.create(generation_request(), scope(client=1, key=7), ADDRESS)

    with pytest.raises(IdempotencyKeyConflictError):
        await harness.create(generation_request("Something else"), scope(client=1, key=7), ADDRESS)

    assert list(harness.store.jobs) == [first.job.job_id]
    assert harness.store.requests[first.job.job_id] == generation_request()
    assert harness.queue.enqueued == [first.job.job_id]


async def test_same_key_with_a_different_request_from_another_client_is_not_a_conflict() -> None:
    harness = Harness()
    first = await harness.create(generation_request(), scope(client=1, key=7), ADDRESS)

    second = await harness.create(generation_request("Something else"), scope(client=2, key=7), ADDRESS)

    assert second.job.job_id != first.job.job_id


async def test_same_key_from_a_different_client_is_a_different_job() -> None:
    harness = Harness()

    first = await harness.create(generation_request(), scope(client=1, key=7), ADDRESS)
    second = await harness.create(generation_request(), scope(client=2, key=7), ADDRESS)

    assert first.job.job_id != second.job.job_id
    assert len(harness.store.jobs) == 2


async def test_different_key_from_the_same_client_is_a_different_job() -> None:
    harness = Harness()

    first = await harness.create(generation_request(), scope(client=1, key=1), ADDRESS)
    second = await harness.create(generation_request(), scope(client=1, key=2), ADDRESS)

    assert first.job.job_id != second.job.job_id


async def test_replay_of_a_job_that_left_the_queue_is_not_enqueued_again() -> None:
    harness = Harness()
    first = await harness.create(generation_request(), scope(), ADDRESS)
    harness.store.replace(first.job.start(harness.now).cancel(harness.now))

    replay = await harness.create(generation_request(), scope(), ADDRESS)

    assert replay.job.status is JobStatus.CANCELLED
    assert harness.queue.enqueued == [first.job.job_id]


async def test_replay_after_a_failed_enqueue_enqueues_the_same_job() -> None:
    harness = Harness()
    harness.queue.unavailable = True
    with pytest.raises(ConnectionError):
        await harness.create(generation_request(), scope(), ADDRESS)
    harness.queue.unavailable = False

    replay = await harness.create(generation_request(), scope(), ADDRESS)

    assert replay.job.job_id == job_id(1)
    assert harness.queue.enqueued == [job_id(1)]
    assert list(harness.store.jobs) == [job_id(1)]


async def test_new_job_uses_one_unit_of_the_client_quota() -> None:
    harness = Harness()

    created = await harness.create(generation_request(), scope(), ADDRESS)

    assert created.quota.remaining == QUOTA_LIMIT - 1
    assert harness.quota.used_by_client[scope().client_id] == 1
    assert harness.quota.used_by_address[ADDRESS] == 1


async def test_replay_reports_the_quota_without_using_it_again() -> None:
    harness = Harness()
    await harness.create(generation_request(), scope(), ADDRESS)

    replay = await harness.create(generation_request(), scope(), ADDRESS)

    assert replay.quota.remaining == QUOTA_LIMIT - 1
    assert replay.quota.resets_at > harness.now
    assert harness.quota.used_by_client[scope().client_id] == 1
    assert harness.quota.released == []


async def test_replay_of_the_job_that_used_up_the_quota_returns_it_instead_of_a_429() -> None:
    harness = Harness()
    harness.quota.per_client = 1
    first = await harness.create(generation_request(), scope(), ADDRESS)

    replay = await harness.create(generation_request(), scope(), ADDRESS)

    assert replay.job == first.job
    assert replay.quota.remaining == 0


async def test_exhausted_client_is_rate_limited_before_anything_is_stored() -> None:
    harness = Harness()
    harness.quota.per_client = 2
    for key in (1, 2):
        await harness.create(generation_request(), scope(key=key), ADDRESS)

    with pytest.raises(RateLimitedError) as raised:
        await harness.create(generation_request(), scope(key=3), ADDRESS)

    assert raised.value.retry_after_seconds == QUOTA_RETRY_AFTER_SECONDS
    assert list(harness.store.jobs) == [job_id(1), job_id(2)]
    assert harness.queue.enqueued == [job_id(1), job_id(2)]


async def test_rotating_client_ids_from_one_address_is_still_rate_limited() -> None:
    harness = Harness()
    harness.quota.per_address = 3
    for client in (1, 2, 3):
        await harness.create(generation_request(), scope(client=client), ADDRESS)

    with pytest.raises(RateLimitedError):
        await harness.create(generation_request(), scope(client=4), ADDRESS)

    created = await harness.create(generation_request(), scope(client=4), OTHER_ADDRESS)
    assert created.quota.remaining == QUOTA_LIMIT - 1
    assert harness.quota.used_by_client[scope(client=4).client_id] == 1


async def test_conflicting_reuse_of_a_key_does_not_use_the_quota() -> None:
    harness = Harness()
    await harness.create(generation_request(), scope(), ADDRESS)

    with pytest.raises(IdempotencyKeyConflictError):
        await harness.create(generation_request("Something else"), scope(), ADDRESS)

    assert harness.quota.used_by_client[scope().client_id] == 1


class RacingJobStore(InMemoryJobStore):
    async def find(self, scope: IdempotencyScope) -> StoredJob | None:
        del scope
        return None


async def racing_harness() -> tuple[Harness, StoredJob]:
    harness = Harness(store=RacingJobStore())
    twin = await harness.store.add(GenerationJob.queue(job_id(99), T0), generation_request(), scope())
    return harness, twin


async def test_request_that_loses_the_race_to_its_twin_gives_its_quota_back() -> None:
    harness, twin = await racing_harness()

    created = await harness.create(generation_request(), scope(), ADDRESS)

    assert created.job == twin.job
    assert harness.quota.released == [Requester(client_id=scope().client_id, address=ADDRESS)]
    assert harness.quota.used_by_client[scope().client_id] == 0
    assert harness.quota.used_by_address[ADDRESS] == 0
    assert created.quota.remaining == QUOTA_LIMIT


async def test_conflict_found_only_after_reserving_gives_the_quota_back() -> None:
    harness, _ = await racing_harness()

    with pytest.raises(IdempotencyKeyConflictError):
        await harness.create(generation_request("Something else"), scope(), ADDRESS)

    assert harness.quota.used_by_client[scope().client_id] == 0


class LateTwinJobStore(InMemoryJobStore):
    def __init__(self) -> None:
        super().__init__()
        self.misses = 1

    async def find(self, scope: IdempotencyScope) -> StoredJob | None:
        if self.misses > 0:
            self.misses -= 1
            return None
        return await super().find(scope)


async def test_twin_that_took_the_last_unit_first_is_returned_instead_of_a_429() -> None:
    harness = Harness(store=LateTwinJobStore())
    harness.quota.per_client = 1
    twin = await harness.store.add(GenerationJob.queue(job_id(99), T0), generation_request(), scope())
    await harness.quota.reserve(Requester(client_id=scope().client_id, address=ADDRESS), T0)

    created = await harness.create(generation_request(), scope(), ADDRESS)

    assert created.job == twin.job
    assert created.quota.remaining == 0


async def test_request_refused_for_quota_is_still_refused_when_no_twin_appears() -> None:
    harness = Harness(store=LateTwinJobStore())
    harness.quota.per_client = 0

    with pytest.raises(RateLimitedError):
        await harness.create(generation_request(), scope(), ADDRESS)

    assert harness.store.jobs == {}


class FailingJobStore(InMemoryJobStore):
    async def add(self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope) -> StoredJob:
        del job, request, scope
        message = "the job store did not answer"
        raise TimeoutError(message)


async def test_job_that_could_not_be_stored_gives_its_quota_back() -> None:
    harness = Harness(store=FailingJobStore())

    with pytest.raises(TimeoutError):
        await harness.create(generation_request(), scope(), ADDRESS)

    assert harness.quota.used_by_client[scope().client_id] == 0
    assert harness.queue.enqueued == []


async def test_stored_job_that_failed_to_enqueue_keeps_its_quota_and_its_replay_is_free() -> None:
    harness = Harness()
    harness.queue.unavailable = True
    with pytest.raises(ConnectionError):
        await harness.create(generation_request(), scope(), ADDRESS)
    harness.queue.unavailable = False

    replay = await harness.create(generation_request(), scope(), ADDRESS)

    assert harness.queue.enqueued == [job_id(1)]
    assert replay.quota.remaining == QUOTA_LIMIT - 1
    assert harness.quota.released == []


async def test_unreadable_quota_refuses_the_job_before_anything_is_stored() -> None:
    harness = Harness()
    harness.quota.failure = UpstreamUnavailableError(1)

    with pytest.raises(UpstreamUnavailableError):
        await harness.create(generation_request(), scope(), ADDRESS)

    assert harness.store.jobs == {}
    assert harness.queue.enqueued == []
