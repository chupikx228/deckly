import asyncio
from dataclasses import replace

import pytest

from deckly.application.exceptions import NoValidContentError, UpstreamUnavailableError
from deckly.domain.deck import GenerationResult
from deckly.domain.exceptions import TopicRejectedError
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import STAGE_ORDER, FailureCode, JobStage
from tests.fakes import ADDRESS, Harness, InMemoryResultCache, generation_request, scope
from tests.logs import LogLine, captured_json_logs

pytestmark = pytest.mark.anyio

WITH_IMAGES = replace(generation_request(), include_images=True)
LIFECYCLE_EVENTS = frozenset(
    {
        "generation_admitted",
        "generation_started",
        "generation_stage_entered",
        "generation_succeeded",
        "generation_failed",
        "generation_stopped",
        "generation_cancelled",
    }
)


def lifecycle(lines: list[LogLine]) -> list[tuple[object, ...]]:
    return [
        (line["message"], line.get("stage"), line.get("status"))
        for line in lines
        if line["message"] in LIFECYCLE_EVENTS
    ]


def sample(harness: Harness, name: str, **labels: str) -> float | None:
    return harness.observability.metrics.registry.get_sample_value(name, labels)


async def test_a_successful_job_is_reconstructable_from_the_lines_carrying_its_job_id() -> None:
    harness = Harness()

    with captured_json_logs() as logs:
        job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id
        await harness.run(job_id)

    assert lifecycle(logs.for_field("job_id", str(job_id))) == [
        ("generation_admitted", None, "queued"),
        ("generation_started", JobStage.PLANNING, "running"),
        *[("generation_stage_entered", stage, "running") for stage in STAGE_ORDER[1:]],
        ("generation_succeeded", None, "succeeded"),
    ]
    [succeeded] = logs.named("generation_succeeded")
    assert isinstance(succeeded["duration_ms"], int)
    assert succeeded["cache_hit"] is False


async def test_every_worker_line_shares_one_trace_and_names_the_job() -> None:
    harness = Harness()
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    with captured_json_logs() as logs:
        await harness.run(job_id)

    lines = logs.lines()
    assert lines
    assert {line.get("job_id") for line in lines} == {str(job_id)}
    assert len({line.get("trace_id") for line in lines}) == 1
    assert None not in {line.get("trace_id") for line in lines}


async def test_a_successful_job_records_throughput_and_the_latency_of_every_stage() -> None:
    harness = Harness()
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    await harness.run(job_id)

    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="queued") == 1
    assert sample(harness, "deckly_generation_jobs_finished_total", outcome="succeeded", failure_code="") == 1
    assert sample(harness, "deckly_generation_job_duration_seconds_count", outcome="succeeded") == 1
    for stage in STAGE_ORDER:
        assert (
            sample(harness, "deckly_generation_stage_duration_seconds_count", stage=stage, outcome="ok") == 1
        )


async def test_a_failing_stage_is_counted_as_a_stage_failure_and_a_failed_job() -> None:
    harness = Harness()
    harness.providers.failures[JobStage.PARSING_SOURCES] = UpstreamUnavailableError(5)
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    with captured_json_logs() as logs:
        await harness.run(job_id)

    assert (
        sample(
            harness,
            "deckly_generation_stage_duration_seconds_count",
            stage="parsing_sources",
            outcome="failed",
        )
        == 1
    )
    assert (
        sample(
            harness, "deckly_generation_stage_duration_seconds_count", stage="generating_cards", outcome="ok"
        )
        is None
    )
    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="failed",
            failure_code=FailureCode.PROVIDER_UNAVAILABLE,
        )
        == 1
    )
    [failed] = logs.named("generation_failed")
    assert failed["job_id"] == str(job_id)
    assert failed["stage"] == JobStage.PARSING_SOURCES
    assert failed["failure_code"] == FailureCode.PROVIDER_UNAVAILABLE
    assert isinstance(failed["duration_ms"], int)


async def test_a_job_that_keeps_no_note_fails_with_its_failure_code_on_the_metric() -> None:
    harness = Harness()
    harness.providers.screen_failure = NoValidContentError("nothing left")
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    await harness.run(job_id)

    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="failed",
            failure_code=FailureCode.NO_VALID_CONTENT,
        )
        == 1
    )


async def test_a_job_cancelled_mid_stage_is_counted_as_cancelled_and_stopped_not_failed() -> None:
    harness = Harness()
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    async def cancel() -> None:
        await harness.cancel(job_id)

    harness.providers.during[JobStage.GENERATING_CARDS] = cancel

    with captured_json_logs() as logs:
        await harness.run(job_id)

    assert sample(harness, "deckly_generation_jobs_cancelled_total") == 1
    assert sample(harness, "deckly_generation_jobs_finished_total", outcome="stopped", failure_code="") == 1
    assert (
        sample(
            harness,
            "deckly_generation_stage_duration_seconds_count",
            stage="fetching_media",
            outcome="stopped",
        )
        == 1
    )
    assert lifecycle(logs.for_field("job_id", str(job_id)))[-2:] == [
        ("generation_cancelled", JobStage.GENERATING_CARDS, None),
        ("generation_stopped", None, None),
    ]


async def test_a_job_the_worker_finds_already_finished_is_counted_as_skipped() -> None:
    harness = Harness()
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id
    await harness.run(job_id)

    await harness.run(job_id)

    assert sample(harness, "deckly_generation_jobs_finished_total", outcome="skipped", failure_code="") == 1


async def test_an_interrupted_job_is_recorded_as_failed_even_though_the_cancellation_propagates() -> None:
    harness = Harness()
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id
    entered = asyncio.Event()

    async def hang() -> None:
        entered.set()
        await asyncio.Event().wait()

    harness.providers.during[JobStage.RETRIEVING_SOURCES] = hang
    running = asyncio.create_task(harness.run(job_id))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="failed",
            failure_code=FailureCode.GENERATION_FAILED,
        )
        == 1
    )
    assert (
        sample(
            harness,
            "deckly_generation_stage_duration_seconds_count",
            stage="retrieving_sources",
            outcome="stopped",
        )
        == 1
    )


async def test_a_replayed_request_is_counted_and_logged_as_a_replay_of_the_same_job() -> None:
    harness = Harness()
    first = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    with captured_json_logs() as logs:
        again = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    assert again == first
    [admitted] = logs.named("generation_admitted")
    assert (admitted["job_id"], admitted["admission"]) == (str(first), "replayed")
    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="replayed") == 1


async def test_a_rejected_topic_is_counted_and_logged_without_the_topic() -> None:
    harness = Harness()
    harness.moderator.outcome = False
    request = replace(WITH_IMAGES, topic="forbidden canary topic")

    with captured_json_logs() as logs, pytest.raises(TopicRejectedError):
        await harness.create(request, scope(), ADDRESS)

    assert [line["message"] for line in logs.lines()] == ["topic_rejected"]
    assert "canary" not in logs.text
    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="topic_rejected") == 1


class HangingCacheWrite(InMemoryResultCache):
    def __init__(self) -> None:
        super().__init__()
        self.writing = asyncio.Event()

    async def put(self, request: GenerationRequest, result: GenerationResult) -> None:
        del request, result
        self.writing.set()
        await asyncio.Event().wait()


async def test_a_job_interrupted_after_its_success_was_stored_is_still_counted_as_succeeded() -> None:
    harness = Harness()
    cache = HangingCacheWrite()
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id
    running = asyncio.create_task(replace(harness.run, cache=cache)(job_id))
    await cache.writing.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert sample(harness, "deckly_generation_jobs_finished_total", outcome="succeeded", failure_code="") == 1
    assert (
        sample(harness, "deckly_generation_jobs_finished_total", outcome="stopped", failure_code="") is None
    )
