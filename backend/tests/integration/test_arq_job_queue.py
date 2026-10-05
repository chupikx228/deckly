from uuid import uuid4

import pytest
from arq.connections import ArqRedis
from arq.constants import abort_jobs_ss
from arq.jobs import Job
from opentelemetry.trace import format_trace_id

from deckly.infrastructure.queue import GENERATION_TASK
from tests.fakes import fresh_observability
from tests.integration.conftest import Cleanup, job_queue

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

    await job_queue(queue_pool).enqueue(job_id)

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
        await job_queue(queue_pool).enqueue(job_id)

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
    await queue.enqueue(job_id)
    first = await Job(str(job_id), queue_pool).info()

    await queue.enqueue(job_id)

    second = await Job(str(job_id), queue_pool).info()
    assert first is not None
    assert second is not None
    assert second.enqueue_time == first.enqueue_time
    assert [job.job_id for job in await queue_pool.queued_jobs()].count(str(job_id)) == 1
