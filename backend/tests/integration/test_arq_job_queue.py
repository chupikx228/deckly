from uuid import uuid4

import pytest
from arq.connections import ArqRedis, create_pool
from arq.constants import abort_jobs_ss, default_queue_name
from arq.jobs import Job
from opentelemetry.trace import format_trace_id

from deckly.application.ports import QueueLane
from deckly.config import Settings
from deckly.infrastructure.queue import GENERATION_TASK, TRANSCRIPTION_QUEUE_NAME
from tests.fakes import fresh_observability
from tests.integration.conftest import Cleanup, job_queue, redis_settings

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_abort_leaves_the_marker_that_arq_workers_poll_for(
    queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    job_id = uuid4()
    cleanup.job_ids.add(job_id)

    await job_queue(queue_pool).abort(job_id)

    assert await queue_pool.zscore(abort_jobs_ss, str(job_id)) is not None


async def test_job_is_enqueued_for_the_generation_task_under_its_own_id(
    queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    job_id = uuid4()
    cleanup.job_ids.add(job_id)

    await job_queue(queue_pool).enqueue(job_id, QueueLane.GENERATION)

    info = await Job(str(job_id), queue_pool).info()
    assert info is not None
    assert info.function == GENERATION_TASK
    assert info.args == (str(job_id), {})


async def test_job_carries_the_trace_context_of_the_request_that_enqueued_it(
    queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    job_id = uuid4()
    cleanup.job_ids.add(job_id)
    tracer = fresh_observability().tracer

    with tracer.start_as_current_span("POST /v1/generations") as span:
        await job_queue(queue_pool).enqueue(job_id, QueueLane.GENERATION)

    info = await Job(str(job_id), queue_pool).info()
    assert info is not None
    _, carrier = info.args
    trace_id = format_trace_id(span.get_span_context().trace_id)
    assert isinstance(carrier, dict)
    assert trace_id in carrier["traceparent"]


async def test_enqueueing_the_same_job_twice_keeps_the_first_entry(
    queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    job_id = uuid4()
    cleanup.job_ids.add(job_id)
    queue = job_queue(queue_pool)
    await queue.enqueue(job_id, QueueLane.GENERATION)
    first = await Job(str(job_id), queue_pool).info()

    await queue.enqueue(job_id, QueueLane.GENERATION)

    second = await Job(str(job_id), queue_pool).info()
    assert first is not None
    assert second is not None
    assert second.enqueue_time == first.enqueue_time
    assert [job.job_id for job in await queue_pool.queued_jobs()].count(str(job_id)) == 1


async def test_recording_job_waits_on_the_transcription_queue_and_never_on_the_generation_queue(
    queue_pool: ArqRedis, cleanup: Cleanup
) -> None:
    job_id = uuid4()
    cleanup.job_ids.add(job_id)
    queue = job_queue(queue_pool)

    await queue.enqueue(job_id, QueueLane.TRANSCRIPTION)

    assert await queue_pool.zscore(TRANSCRIPTION_QUEUE_NAME, str(job_id)) is not None
    assert await queue_pool.zscore(default_queue_name, str(job_id)) is None
    info = await Job(str(job_id), queue_pool, _queue_name=TRANSCRIPTION_QUEUE_NAME).info()
    assert info is not None
    assert info.function == GENERATION_TASK
    assert await queue.depth(QueueLane.TRANSCRIPTION) >= 1


async def test_generation_job_waits_on_the_queue_its_pool_was_opened_for(
    settings: Settings, cleanup: Cleanup
) -> None:
    queue_name = f"{default_queue_name}:{uuid4()}"
    pool = await create_pool(redis_settings(settings), default_queue_name=queue_name)
    job_id = uuid4()
    cleanup.job_ids.add(job_id)
    try:
        await job_queue(pool).enqueue(job_id, QueueLane.GENERATION)

        assert await pool.zscore(queue_name, str(job_id)) is not None
        assert await job_queue(pool).depth(QueueLane.GENERATION) == 1
    finally:
        await pool.delete(queue_name)
        await pool.aclose()
