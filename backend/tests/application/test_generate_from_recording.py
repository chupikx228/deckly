from dataclasses import replace
from uuid import UUID

import pytest

from deckly.application.ports import IdempotencyScope, QueueLane
from deckly.domain.exceptions import TopicRejectedError
from deckly.domain.generation import (
    DocumentGenerationFingerprint,
    GenerationFingerprint,
    GenerationRequest,
    RecordingGenerationFingerprint,
)
from deckly.domain.job import STAGE_ORDER, Cancelled, Failed, FailureCode, JobStage, JobStatus, Succeeded
from tests.fakes import (
    ADDRESS,
    DOCUMENT_CLIENT_ID,
    DOCUMENT_ID,
    GENERATED,
    RECORDING_CONTENT,
    TRANSCRIPT_MATERIAL,
    UPLOADED_DOCUMENT,
    UPLOADED_RECORDING,
    Harness,
    document_request,
    generation_request,
)
from tests.logs import captured_json_logs

pytestmark = pytest.mark.anyio

OTHER_DOCUMENT_ID = UUID("6e1d8b2f-4c3a-4d9e-8f7a-2b3c4d5e6f70")
LIFECYCLE_LINES = frozenset(
    {"generation_admitted", "generation_started", "generation_stage_entered", "generation_succeeded"}
)


def owner_scope(key: int = 1) -> IdempotencyScope:
    return IdempotencyScope(client_id=DOCUMENT_CLIENT_ID, idempotency_key=UUID(int=key, version=4))


def harness_with_recording() -> Harness:
    harness = Harness()
    harness.documents.keep(UPLOADED_RECORDING, RECORDING_CONTENT)
    harness.documents.material = TRANSCRIPT_MATERIAL
    return harness


async def queued(harness: Harness, request: GenerationRequest | None = None, key: int = 1) -> UUID:
    created = await harness.create(request or document_request(), owner_scope(key), ADDRESS)
    return created.job.job_id


def sample(harness: Harness, name: str, **labels: str) -> float | None:
    return harness.observability.metrics.registry.get_sample_value(name, labels)


def failure_of(harness: Harness, job_id: UUID) -> Failed:
    state = harness.store.jobs[job_id].state
    assert isinstance(state, Failed)
    return state


async def test_recording_request_is_queued_for_transcription_and_counted_as_a_recording() -> None:
    harness = harness_with_recording()

    with captured_json_logs() as logs:
        job_id = await queued(harness)

    assert harness.queue.enqueued == [job_id]
    assert harness.queue.lanes == [QueueLane.TRANSCRIPTION]
    [admitted] = logs.named("generation_admitted")
    assert admitted["source_kind"] == "recording"
    assert admitted["document_id"] == str(DOCUMENT_ID)
    admitted_total = "deckly_generation_jobs_admitted_total"
    assert sample(harness, admitted_total, outcome="queued", source="recording") == 1
    assert sample(harness, admitted_total, outcome="queued", source="document") is None


async def test_recording_job_polls_through_every_stage_and_writes_cards_from_its_transcript() -> None:
    harness = harness_with_recording()
    job_id = await queued(harness)
    polled_before = await harness.get(job_id)

    await harness.run(job_id)

    polled_after = await harness.get(job_id)
    assert polled_before.status is JobStatus.QUEUED
    assert isinstance(polled_after.state, Succeeded)
    assert polled_after.state.result == GENERATED
    stages = [job.stage for job in harness.store.history if job.stage is not None]
    assert stages == [JobStage.PLANNING, *STAGE_ORDER[1:4], JobStage.FINALIZING]
    assert harness.providers.retrieved_for == []
    assert harness.documents.storage.reads == [
        (UPLOADED_RECORDING.storage_key, UPLOADED_RECORDING.size_bytes)
    ]
    assert harness.documents.parsed == [(UPLOADED_RECORDING, RECORDING_CONTENT)]
    assert harness.providers.received[JobStage.GENERATING_CARDS] == TRANSCRIPT_MATERIAL
    assert harness.providers.subjects == [UPLOADED_RECORDING.filename]


async def test_recording_job_reads_its_record_once_at_admission_and_once_in_the_worker() -> None:
    harness = harness_with_recording()
    job_id = await queued(harness)

    await harness.run(job_id)

    assert harness.documents.lookups == [(DOCUMENT_ID, DOCUMENT_CLIENT_ID)] * 2


async def test_recording_job_lifecycle_lines_and_metrics_are_labelled_as_a_recording() -> None:
    harness = harness_with_recording()

    with captured_json_logs() as logs:
        job_id = await queued(harness)
        await harness.run(job_id)

    lifecycle = [line for line in logs.for_field("job_id", str(job_id)) if line["message"] in LIFECYCLE_LINES]
    assert len(lifecycle) == 7
    assert {line["source_kind"] for line in lifecycle} == {"recording"}
    assert {line["document_id"] for line in lifecycle} == {str(DOCUMENT_ID)}
    finished = {"outcome": "succeeded", "failure_code": ""}
    assert sample(harness, "deckly_generation_jobs_finished_total", **finished, source="recording") == 1
    assert sample(harness, "deckly_generation_jobs_finished_total", **finished, source="document") is None
    assert (
        sample(
            harness, "deckly_generation_job_duration_seconds_count", outcome="succeeded", source="recording"
        )
        == 1
    )
    for stage in (JobStage.PLANNING, *STAGE_ORDER[1:4], JobStage.FINALIZING):
        assert (
            sample(
                harness,
                "deckly_generation_stage_duration_seconds_count",
                stage=stage,
                outcome="ok",
                source="recording",
            )
            == 1
        )


async def test_queued_recording_job_cancelled_before_a_worker_takes_it_is_never_downloaded() -> None:
    harness = harness_with_recording()
    job_id = await queued(harness)
    await harness.cancel(job_id)

    await harness.run(job_id)

    assert isinstance(harness.store.jobs[job_id].state, Cancelled)
    assert harness.queue.aborted == [job_id]
    assert harness.documents.storage.reads == []
    assert harness.documents.parsed == []


async def test_recording_job_cancelled_while_it_is_transcribed_stops_without_writing_cards() -> None:
    harness = harness_with_recording()
    job_id = await queued(harness)
    harness.documents.during["parse"] = lambda: harness.cancel(job_id)

    with captured_json_logs() as logs:
        await harness.run(job_id)

    state = harness.store.jobs[job_id].state
    assert isinstance(state, Cancelled)
    assert state.stage is JobStage.PARSING_SOURCES
    assert harness.providers.generated_for == []
    assert harness.cache.writes == []
    assert logs.named("generation_stopped")


async def test_recording_request_is_screened_at_admission_with_the_recording_and_its_instructions() -> None:
    harness = harness_with_recording()
    request = replace(document_request(), instructions="Only the second half")

    await queued(harness, request)

    assert harness.moderator.screened == [request]
    assert harness.moderator.documents == [UPLOADED_RECORDING]


async def test_recording_request_whose_title_or_instructions_break_the_policy_is_rejected_at_admission() -> (
    None
):
    harness = harness_with_recording()
    harness.moderator.outcome = False

    with captured_json_logs() as logs, pytest.raises(TopicRejectedError):
        await queued(harness)

    assert harness.store.jobs == {}
    assert harness.queue.enqueued == []
    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 1
    [rejected] = logs.named("topic_rejected")
    assert rejected["source_kind"] == "recording"
    assert (
        sample(harness, "deckly_generation_jobs_admitted_total", outcome="topic_rejected", source="recording")
        == 1
    )


async def test_whole_transcript_is_screened_in_the_worker_before_any_card_is_written() -> None:
    harness = harness_with_recording()
    job_id = await queued(harness)

    await harness.run(job_id)

    assert harness.documents.screened == [(job_id, UPLOADED_RECORDING, TRANSCRIPT_MATERIAL)]


async def test_recording_whose_transcript_breaks_the_policy_is_rejected_before_any_card_is_written() -> None:
    harness = harness_with_recording()
    harness.documents.allowed = False
    job_id = await queued(harness)

    with captured_json_logs() as logs:
        await harness.run(job_id)

    failure = failure_of(harness, job_id)
    assert failure.code is FailureCode.DOCUMENT_REJECTED
    assert failure.stage is JobStage.PARSING_SOURCES
    assert harness.providers.generated_for == []
    assert harness.cache.writes == []
    [failed] = logs.named("generation_failed")
    assert failed["source_kind"] == "recording"
    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="failed",
            failure_code="DOCUMENT_REJECTED",
            source="recording",
        )
        == 1
    )


async def test_second_recording_job_with_the_same_settings_is_served_from_the_recording_cache() -> None:
    harness = harness_with_recording()
    await harness.run(await queued(harness))
    second = await queued(harness, key=2)

    await harness.run(second)

    state = harness.store.jobs[second].state
    assert isinstance(state, Succeeded)
    assert state.result == GENERATED
    assert len(harness.documents.parsed) == 1
    [fingerprint] = harness.cache.entries
    assert isinstance(fingerprint, RecordingGenerationFingerprint)
    assert fingerprint == document_request().fingerprint(UPLOADED_RECORDING)


async def test_a_document_result_cached_under_the_recordings_id_and_settings_is_never_served_for_it() -> None:
    harness = harness_with_recording()
    stale_document_result = replace(GENERATED, notes=GENERATED.notes[:1])
    harness.cache.entries[document_request().fingerprint(UPLOADED_DOCUMENT)] = stale_document_result
    job_id = await queued(harness)

    await harness.run(job_id)

    state = harness.store.jobs[job_id].state
    assert isinstance(state, Succeeded)
    assert state.result == GENERATED
    assert harness.documents.parsed == [(UPLOADED_RECORDING, RECORDING_CONTENT)]


async def test_topic_document_and_recording_jobs_with_the_same_settings_each_keep_their_own_entry() -> None:
    harness = harness_with_recording()
    harness.documents.keep(replace(UPLOADED_DOCUMENT, document_id=OTHER_DOCUMENT_ID))
    await harness.run(await queued(harness, generation_request(), key=1))
    await harness.run(await queued(harness, document_request(OTHER_DOCUMENT_ID), key=2))
    await harness.run(await queued(harness, document_request(), key=3))

    assert len(harness.providers.generated_for) == 3
    assert sorted(type(fingerprint).__name__ for fingerprint in harness.cache.entries) == sorted(
        [
            GenerationFingerprint.__name__,
            DocumentGenerationFingerprint.__name__,
            RecordingGenerationFingerprint.__name__,
        ]
    )


async def test_replayed_recording_request_is_counted_as_a_recording() -> None:
    harness = harness_with_recording()
    await queued(harness)

    await queued(harness)

    assert (
        sample(harness, "deckly_generation_jobs_admitted_total", outcome="replayed", source="recording") == 1
    )


async def test_recording_job_found_running_on_redelivery_is_abandoned_and_counted_as_a_recording() -> None:
    harness = harness_with_recording()
    job_id = await queued(harness)
    harness.store.replace(harness.store.jobs[job_id].start(harness.now))

    with captured_json_logs() as logs:
        await harness.run(job_id)

    assert failure_of(harness, job_id).code is FailureCode.GENERATION_FAILED
    [abandoned] = logs.named("generation_abandoned")
    assert abandoned["source_kind"] == "recording"
    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="failed",
            failure_code="GENERATION_FAILED",
            source="recording",
        )
        == 1
    )


async def test_record_unreadable_when_the_worker_starts_fails_the_job_instead_of_leaving_it_running() -> None:
    harness = harness_with_recording()
    job_id = await queued(harness)
    harness.documents.failures["lookup"] = TimeoutError("document store did not answer")

    with captured_json_logs() as logs:
        await harness.run(job_id)

    failure = failure_of(harness, job_id)
    assert failure.code is FailureCode.GENERATION_FAILED
    assert failure.stage is JobStage.PLANNING
    assert harness.cache.reads == []
    assert harness.documents.storage.reads == []
    [failed] = logs.named("generation_failed")
    assert failed["document_id"] == str(DOCUMENT_ID)
    assert "source_kind" not in failed
