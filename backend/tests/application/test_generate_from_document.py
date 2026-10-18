from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from deckly.application.exceptions import (
    DocumentHasNoTextError,
    EncryptedDocumentError,
    IdempotencyKeyConflictError,
    UnreadableDocumentError,
    UpstreamUnavailableError,
)
from deckly.application.ports import IdempotencyScope, QueueLane, StoredJob
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import (
    DocumentNotFoundError,
    DocumentNotSupportedForGenerationError,
    TopicRejectedError,
)
from deckly.domain.generation import GenerationRequest
from deckly.domain.job import STAGE_ORDER, Cancelled, Failed, FailureCode, GenerationJob, JobStage, Succeeded
from tests.fakes import (
    ADDRESS,
    DOCUMENT_CLIENT_ID,
    DOCUMENT_CONTENT,
    DOCUMENT_ID,
    DOCUMENT_MATERIAL,
    GENERATED,
    UPLOADED_DOCUMENT,
    Harness,
    InMemoryJobStore,
    document_request,
    generation_request,
    scope,
    with_images,
)
from tests.logs import captured_json_logs

pytestmark = pytest.mark.anyio

OTHER_DOCUMENT_ID = UUID("6e1d8b2f-4c3a-4d9e-8f7a-2b3c4d5e6f70")
DOCUMENT_STAGES = (JobStage.RETRIEVING_SOURCES, JobStage.PARSING_SOURCES)
SOURCE_LABELLED_METRICS = (
    "deckly_generation_jobs_admitted",
    "deckly_generation_jobs_finished",
    "deckly_generation_job_duration",
    "deckly_generation_stage_duration",
)


def owner_scope(key: int = 1) -> IdempotencyScope:
    return IdempotencyScope(client_id=DOCUMENT_CLIENT_ID, idempotency_key=UUID(int=key, version=4))


def harness_with_document() -> Harness:
    harness = Harness()
    harness.documents.keep()
    return harness


async def queued(harness: Harness, request: GenerationRequest | None = None) -> UUID:
    created = await harness.create(request or document_request(), owner_scope(), ADDRESS)
    return created.job.job_id


def failure_of(harness: Harness, job_id: UUID) -> Failed:
    state = harness.store.jobs[job_id].state
    assert isinstance(state, Failed)
    return state


def sample(harness: Harness, name: str, **labels: str) -> float | None:
    if name.startswith(SOURCE_LABELLED_METRICS):
        labels = {"source": "document", **labels}
    return harness.observability.metrics.registry.get_sample_value(name, labels)


class RacingJobStore(InMemoryJobStore):
    async def add(self, job: GenerationJob, request: GenerationRequest, scope: IdempotencyScope) -> StoredJob:
        del job, scope
        message = f"document {request.document_id} was swept while the request was screened"
        raise DocumentNotFoundError(message)


async def test_request_from_a_stored_document_is_screened_with_it_and_queued() -> None:
    harness = harness_with_document()

    created = await harness.create(document_request(), owner_scope(), ADDRESS)

    assert harness.queue.enqueued == [created.job.job_id]
    assert harness.store.requests[created.job.job_id] == document_request()
    assert harness.moderator.screened == [document_request()]
    assert harness.moderator.documents == [UPLOADED_DOCUMENT]
    assert harness.documents.lookups == [(DOCUMENT_ID, DOCUMENT_CLIENT_ID)]
    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 1


async def test_topic_request_is_screened_without_a_document_and_never_looks_one_up() -> None:
    harness = Harness()

    await harness.create(generation_request(), scope(), ADDRESS)

    assert harness.moderator.documents == [None]
    assert harness.documents.lookups == []


@pytest.mark.parametrize("uploaded_by", ["nobody", "another client"])
async def test_document_the_client_cannot_use_is_refused_before_the_quota_or_the_screen(
    uploaded_by: str,
) -> None:
    harness = Harness()
    if uploaded_by == "another client":
        harness.documents.keep(replace(UPLOADED_DOCUMENT, client_id=UUID(int=99, version=4)))

    with captured_json_logs() as logs, pytest.raises(DocumentNotFoundError):
        await harness.create(document_request(), owner_scope(), ADDRESS)

    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 0
    assert harness.quota.released == []
    assert harness.moderator.screened == []
    assert harness.store.jobs == {}
    assert harness.queue.enqueued == []
    [refused] = logs.named("generation_document_not_found")
    assert refused["document_id"] == str(DOCUMENT_ID)
    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="document_not_found") == 1


RECORDING_FORMATS = [
    DocumentFormat.MP3,
    DocumentFormat.M4A,
    DocumentFormat.WAV,
    DocumentFormat.MP4,
    DocumentFormat.MOV,
]


def harness_with_recording(
    document_format: DocumentFormat = DocumentFormat.M4A, *, transcription_enabled: bool = True
) -> Harness:
    harness = Harness(transcription_enabled=transcription_enabled)
    harness.documents.keep(replace(UPLOADED_DOCUMENT, document_format=document_format, duration_seconds=60.0))
    return harness


@pytest.mark.parametrize("document_format", RECORDING_FORMATS)
async def test_recording_is_queued_for_the_transcription_worker_and_nothing_else_is(
    document_format: DocumentFormat,
) -> None:
    harness = harness_with_recording(document_format)

    created = await harness.create(document_request(), owner_scope(), ADDRESS)

    assert harness.queue.enqueued == [created.job.job_id]
    assert harness.queue.lanes == [QueueLane.TRANSCRIPTION]
    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 1
    [screened] = harness.moderator.documents
    assert screened is not None
    assert screened.document_format is document_format


@pytest.mark.parametrize("request_kind", ["topic", "document"])
async def test_topic_and_document_requests_stay_on_the_generation_queue(request_kind: str) -> None:
    harness = harness_with_document()
    request = generation_request() if request_kind == "topic" else document_request()

    await harness.create(request, owner_scope(), ADDRESS)

    assert harness.queue.lanes == [QueueLane.GENERATION]


async def test_replayed_request_for_a_queued_recording_job_is_requeued_on_the_transcription_queue() -> None:
    harness = harness_with_recording()
    created = await harness.create(document_request(), owner_scope(), ADDRESS)

    replayed = await harness.create(document_request(), owner_scope(), ADDRESS)

    assert replayed.job.job_id == created.job.job_id
    assert harness.queue.enqueued == [created.job.job_id, created.job.job_id]
    assert harness.queue.lanes == [QueueLane.TRANSCRIPTION, QueueLane.TRANSCRIPTION]


async def test_replayed_queued_job_whose_document_is_gone_falls_back_to_the_generation_queue() -> None:
    harness = harness_with_recording()
    created = await harness.create(document_request(), owner_scope(), ADDRESS)
    harness.documents.documents.clear()

    await harness.create(document_request(), owner_scope(), ADDRESS)

    assert harness.queue.enqueued == [created.job.job_id, created.job.job_id]
    assert harness.queue.lanes == [QueueLane.TRANSCRIPTION, QueueLane.GENERATION]


@pytest.mark.parametrize("document_format", RECORDING_FORMATS)
async def test_recording_is_refused_before_the_quota_or_the_screen_where_transcription_is_disabled(
    document_format: DocumentFormat,
) -> None:
    harness = harness_with_recording(document_format, transcription_enabled=False)

    with captured_json_logs() as logs, pytest.raises(DocumentNotSupportedForGenerationError):
        await harness.create(document_request(), owner_scope(), ADDRESS)

    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 0
    assert harness.quota.released == []
    assert harness.moderator.screened == []
    assert harness.store.jobs == {}
    assert harness.queue.enqueued == []
    [refused] = logs.named("generation_document_not_supported")
    assert refused["document_id"] == str(DOCUMENT_ID)
    assert refused["content_type"] == document_format
    assert (
        sample(
            harness,
            "deckly_generation_jobs_admitted_total",
            outcome="document_not_supported",
            source="recording",
        )
        == 1
    )


async def test_document_swept_while_the_request_was_screened_releases_the_quota() -> None:
    harness = Harness(store=RacingJobStore())
    harness.documents.keep()

    with pytest.raises(DocumentNotFoundError):
        await harness.create(document_request(), owner_scope(), ADDRESS)

    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 0
    assert len(harness.quota.released) == 1
    assert harness.queue.enqueued == []
    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="document_not_found") == 1


async def test_request_whose_file_name_or_instructions_break_the_policy_is_rejected_like_a_topic() -> None:
    harness = harness_with_document()
    harness.moderator.outcome = False

    with pytest.raises(TopicRejectedError):
        await harness.create(document_request(), owner_scope(), ADDRESS)

    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 1
    assert harness.quota.released == []
    assert harness.store.jobs == {}
    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="topic_rejected") == 1


async def test_replaying_a_document_request_returns_the_original_job_and_a_different_document_conflicts() -> (
    None
):
    harness = harness_with_document()
    first = await harness.create(document_request(), owner_scope(key=7), ADDRESS)
    harness.now += timedelta(seconds=30)

    replay = await harness.create(document_request(), owner_scope(key=7), ADDRESS)
    with pytest.raises(IdempotencyKeyConflictError):
        await harness.create(document_request(OTHER_DOCUMENT_ID), owner_scope(key=7), ADDRESS)
    with pytest.raises(IdempotencyKeyConflictError):
        await harness.create(generation_request(), owner_scope(key=7), ADDRESS)

    assert replay.job == first.job
    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 1
    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="replayed") == 1


async def test_document_job_reads_parses_and_screens_the_document_instead_of_searching_the_web() -> None:
    harness = harness_with_document()
    job_id = await queued(harness)

    await harness.run(job_id)

    state = harness.store.jobs[job_id].state
    assert isinstance(state, Succeeded)
    assert harness.providers.retrieved_for == []
    assert harness.providers.parsed_for == []
    assert harness.documents.storage.reads == [(UPLOADED_DOCUMENT.storage_key, UPLOADED_DOCUMENT.size_bytes)]
    assert harness.documents.parsed == [(UPLOADED_DOCUMENT, DOCUMENT_CONTENT)]
    assert harness.documents.screened == [(job_id, UPLOADED_DOCUMENT, DOCUMENT_MATERIAL)]
    assert harness.providers.received[JobStage.GENERATING_CARDS] == DOCUMENT_MATERIAL
    assert harness.providers.subjects == [UPLOADED_DOCUMENT.filename]


async def test_document_job_reads_its_document_as_the_client_that_asked_for_it() -> None:
    harness = harness_with_document()
    job_id = await queued(harness)

    await harness.run(job_id)

    assert harness.documents.lookups == [(DOCUMENT_ID, DOCUMENT_CLIENT_ID)] * 2


async def test_document_job_moves_through_every_stage_in_order() -> None:
    harness = harness_with_document()
    job_id = await queued(harness)

    await harness.run(job_id)

    stages = [job.stage for job in harness.store.history if job.stage is not None]
    assert stages == [JobStage.PLANNING, *STAGE_ORDER[1:4], JobStage.FINALIZING]


@pytest.mark.parametrize(
    ("missing", "stage"),
    [("record", JobStage.PLANNING), ("object", JobStage.RETRIEVING_SOURCES)],
)
async def test_document_gone_before_the_worker_reached_it_fails_as_document_not_found(
    missing: str, stage: JobStage
) -> None:
    harness = harness_with_document()
    job_id = await queued(harness)
    if missing == "record":
        harness.documents.documents.clear()
    else:
        harness.documents.storage.objects.clear()

    await harness.run(job_id)

    failure = failure_of(harness, job_id)
    assert failure.code is FailureCode.DOCUMENT_NOT_FOUND
    assert failure.stage is stage
    assert harness.documents.parsed == []
    assert harness.providers.generated_for == []


async def test_document_record_gone_before_the_worker_reached_it_fails_without_consulting_the_cache() -> None:
    harness = harness_with_document()
    job_id = await queued(harness)
    harness.cache.entries[document_request().fingerprint(UPLOADED_DOCUMENT)] = GENERATED
    harness.documents.documents.clear()

    await harness.run(job_id)

    assert failure_of(harness, job_id).code is FailureCode.DOCUMENT_NOT_FOUND
    assert harness.cache.reads == []
    assert harness.documents.storage.reads == []


async def test_storage_outage_while_reading_the_document_is_provider_unavailable() -> None:
    harness = harness_with_document()
    harness.documents.storage.failure = UpstreamUnavailableError(5)
    job_id = await queued(harness)

    await harness.run(job_id)

    assert failure_of(harness, job_id).code is FailureCode.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (UnreadableDocumentError("damaged"), FailureCode.DOCUMENT_UNREADABLE),
        (EncryptedDocumentError("password"), FailureCode.DOCUMENT_ENCRYPTED),
        (DocumentHasNoTextError("scan"), FailureCode.DOCUMENT_HAS_NO_TEXT),
    ],
    ids=["unreadable", "encrypted", "no text"],
)
async def test_document_the_parser_cannot_read_fails_with_its_document_code(
    error: Exception, code: FailureCode
) -> None:
    harness = harness_with_document()
    harness.documents.failures["parse"] = error
    job_id = await queued(harness)

    await harness.run(job_id)

    failure = failure_of(harness, job_id)
    assert failure.code is code
    assert failure.stage is JobStage.PARSING_SOURCES
    assert harness.documents.screened == []


async def test_document_whose_text_breaks_the_policy_is_rejected_before_any_card_is_written() -> None:
    harness = harness_with_document()
    harness.documents.allowed = False
    job_id = await queued(harness)

    with captured_json_logs() as logs:
        await harness.run(job_id)

    failure = failure_of(harness, job_id)
    assert failure.code is FailureCode.DOCUMENT_REJECTED
    assert failure.stage is JobStage.PARSING_SOURCES
    assert harness.providers.generated_for == []
    [failed] = logs.named("generation_failed")
    assert failed["level"] == "WARNING"
    assert failed["source_kind"] == "document"
    assert failed["document_id"] == str(DOCUMENT_ID)


async def test_outage_of_the_document_screen_fails_the_job_as_provider_unavailable_and_writes_no_card() -> (
    None
):
    harness = harness_with_document()
    harness.documents.failures["screen"] = UpstreamUnavailableError(5)
    job_id = await queued(harness)

    await harness.run(job_id)

    assert failure_of(harness, job_id).code is FailureCode.PROVIDER_UNAVAILABLE
    assert harness.providers.generated_for == []


async def test_document_job_cancelled_while_its_document_is_parsed_stops_without_generating() -> None:
    harness = harness_with_document()
    job_id = await queued(harness)
    harness.documents.during["parse"] = lambda: harness.cancel(job_id)

    with captured_json_logs() as logs:
        await harness.run(job_id)

    assert isinstance(harness.store.jobs[job_id].state, Cancelled)
    assert harness.queue.aborted == [job_id]
    assert harness.providers.generated_for == []
    assert logs.named("generation_stopped")


async def test_second_document_job_with_the_same_settings_is_served_from_the_cache() -> None:
    harness = harness_with_document()
    first = await queued(harness)
    await harness.run(first)
    second = (await harness.create(document_request(), owner_scope(key=2), ADDRESS)).job.job_id

    await harness.run(second)

    state = harness.store.jobs[second].state
    assert isinstance(state, Succeeded)
    assert state.result == GENERATED
    assert len(harness.documents.parsed) == 1
    assert harness.cache.entries == {document_request().fingerprint(UPLOADED_DOCUMENT): GENERATED}


async def test_document_and_topic_jobs_never_share_a_cached_result() -> None:
    harness = harness_with_document()
    topic_job = (await harness.create(generation_request(), scope(key=5), ADDRESS)).job.job_id
    await harness.run(topic_job)
    document_job = await queued(harness)

    await harness.run(document_job)

    assert len(harness.documents.parsed) == 1
    assert set(harness.cache.entries) == {
        generation_request().fingerprint(),
        document_request().fingerprint(UPLOADED_DOCUMENT),
    }


async def test_another_document_with_the_same_settings_is_generated_afresh() -> None:
    harness = harness_with_document()
    harness.documents.keep(replace(UPLOADED_DOCUMENT, document_id=OTHER_DOCUMENT_ID))
    await harness.run(await queued(harness))
    other = (
        await harness.create(document_request(OTHER_DOCUMENT_ID), owner_scope(key=2), ADDRESS)
    ).job.job_id

    await harness.run(other)

    assert [document.document_id for document, _ in harness.documents.parsed] == [
        DOCUMENT_ID,
        OTHER_DOCUMENT_ID,
    ]


async def test_document_job_lifecycle_is_reconstructable_from_its_lines_and_labelled_as_a_document() -> None:
    harness = harness_with_document()

    with captured_json_logs() as logs:
        job_id = await queued(harness)
        await harness.run(job_id)

    lifecycle = [
        line
        for line in logs.for_field("job_id", str(job_id))
        if line["message"]
        in {"generation_admitted", "generation_started", "generation_stage_entered", "generation_succeeded"}
    ]
    assert [line.get("stage") for line in lifecycle] == [
        None,
        JobStage.PLANNING,
        *STAGE_ORDER[1:4],
        JobStage.FINALIZING,
        None,
    ]
    assert {line["source_kind"] for line in lifecycle} == {"document"}
    assert {line["document_id"] for line in lifecycle} == {str(DOCUMENT_ID)}
    [fetched] = logs.named("document_fetched")
    assert fetched["job_id"] == str(job_id)
    assert fetched["size_bytes"] == len(DOCUMENT_CONTENT)


async def test_document_job_metrics_carry_the_document_source_label() -> None:
    harness = harness_with_document()
    job_id = await queued(harness)

    await harness.run(job_id)

    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="queued") == 1
    assert sample(harness, "deckly_generation_jobs_finished_total", outcome="succeeded", failure_code="") == 1
    assert sample(harness, "deckly_generation_job_duration_seconds_count", outcome="succeeded") == 1
    for stage in (JobStage.PLANNING, *DOCUMENT_STAGES, JobStage.GENERATING_CARDS, JobStage.FINALIZING):
        assert (
            sample(harness, "deckly_generation_stage_duration_seconds_count", stage=stage, outcome="ok") == 1
        )
    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="succeeded",
            failure_code="",
            source="topic",
        )
        is None
    )


async def test_document_job_with_images_illustrates_the_cards_written_from_the_document() -> None:
    harness = harness_with_document()
    job_id = await queued(harness, replace(document_request(), include_images=True))

    await harness.run(job_id)

    state = harness.store.jobs[job_id].state
    assert isinstance(state, Succeeded)
    assert state.result.notes == with_images(GENERATED.notes)
    assert harness.providers.fetched_for == [job_id]
    assert (
        sample(
            harness, "deckly_generation_stage_duration_seconds_count", stage="fetching_media", outcome="ok"
        )
        == 1
    )
