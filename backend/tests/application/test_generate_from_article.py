from dataclasses import replace
from uuid import UUID

import pytest

from deckly.application.ports import IdempotencyScope, QueueLane, SourceMaterial
from deckly.domain.exceptions import TopicRejectedError
from deckly.domain.generation import DocumentGenerationFingerprint, GenerationRequest
from deckly.domain.job import STAGE_ORDER, Cancelled, Failed, FailureCode, JobStage, JobStatus, Succeeded
from deckly.domain.source import WebSource
from deckly.infrastructure.documents.parser import DocumentTextParser
from tests.fakes import (
    ADDRESS,
    ARTICLE_CONTENT,
    ARTICLE_MATERIAL,
    ARTICLE_URL,
    DOCUMENT_CLIENT_ID,
    DOCUMENT_ID,
    GENERATED,
    UPLOADED_ARTICLE,
    UPLOADED_DOCUMENT,
    Harness,
    document_request,
)
from tests.logs import captured_json_logs

pytestmark = pytest.mark.anyio

LIFECYCLE_LINES = frozenset(
    {"generation_admitted", "generation_started", "generation_stage_entered", "generation_succeeded"}
)
ARTICLE_STAGES = (JobStage.PLANNING, *STAGE_ORDER[1:4], JobStage.FINALIZING)
PAGE_TEXT = (
    "A red triangle warns of danger ahead. A blue circle gives a mandatory instruction. "
    "A white bar on red means no entry for any vehicle."
)


def owner_scope(key: int = 1) -> IdempotencyScope:
    return IdempotencyScope(client_id=DOCUMENT_CLIENT_ID, idempotency_key=UUID(int=key, version=4))


def harness_with_article(*, transcription_enabled: bool = True) -> Harness:
    harness = Harness(transcription_enabled=transcription_enabled)
    harness.documents.keep(UPLOADED_ARTICLE, ARTICLE_CONTENT)
    harness.documents.material = ARTICLE_MATERIAL
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


async def test_article_request_is_queued_for_generation_and_counted_as_an_article() -> None:
    harness = harness_with_article()

    with captured_json_logs() as logs:
        job_id = await queued(harness)

    assert harness.queue.enqueued == [job_id]
    assert harness.queue.lanes == [QueueLane.GENERATION]
    [admitted] = logs.named("generation_admitted")
    assert admitted["source_kind"] == "article"
    assert admitted["document_id"] == str(DOCUMENT_ID)
    admitted_total = "deckly_generation_jobs_admitted_total"
    assert sample(harness, admitted_total, outcome="queued", source="article") == 1
    assert sample(harness, admitted_total, outcome="queued", source="document") is None


async def test_article_is_admitted_where_transcription_is_disabled() -> None:
    harness = harness_with_article(transcription_enabled=False)

    job_id = await queued(harness)

    assert harness.queue.enqueued == [job_id]
    assert harness.queue.lanes == [QueueLane.GENERATION]


async def test_article_job_polls_through_every_stage_and_writes_cards_from_its_page_text() -> None:
    harness = harness_with_article()
    job_id = await queued(harness)
    polled_before = await harness.get(job_id)

    await harness.run(job_id)

    polled_after = await harness.get(job_id)
    assert polled_before.status is JobStatus.QUEUED
    assert isinstance(polled_after.state, Succeeded)
    assert polled_after.state.result == GENERATED
    stages = [job.stage for job in harness.store.history if job.stage is not None]
    assert stages == list(ARTICLE_STAGES)
    assert harness.providers.retrieved_for == []
    assert harness.documents.storage.reads == [(UPLOADED_ARTICLE.storage_key, UPLOADED_ARTICLE.size_bytes)]
    assert harness.documents.parsed == [(UPLOADED_ARTICLE, ARTICLE_CONTENT)]
    assert harness.providers.received[JobStage.GENERATING_CARDS] == ARTICLE_MATERIAL
    assert harness.providers.subjects == [UPLOADED_ARTICLE.filename]


async def test_article_job_hands_the_generator_its_stored_text_cited_to_the_page_it_was_fetched_from() -> (
    None
):
    harness = harness_with_article()
    harness.documents.keep(UPLOADED_ARTICLE, PAGE_TEXT.encode())
    parser = DocumentTextParser(max_characters=10_000, deadline_seconds=5)
    run = replace(harness.run, documents=replace(harness.run.documents, parser=parser))
    job_id = await queued(harness)

    await run(job_id)

    assert isinstance(harness.store.jobs[job_id].state, Succeeded)
    cited = WebSource(
        title=UPLOADED_ARTICLE.filename, url=ARTICLE_URL, retrieved_at=UPLOADED_ARTICLE.uploaded_at
    )
    expected = (SourceMaterial(source=cited, text=PAGE_TEXT),)
    assert harness.providers.received[JobStage.GENERATING_CARDS] == expected
    [(_, screened_document, screened_material)] = harness.documents.screened
    assert screened_document == UPLOADED_ARTICLE
    assert screened_material == expected


async def test_article_job_lifecycle_lines_and_metrics_are_labelled_as_an_article() -> None:
    harness = harness_with_article()

    with captured_json_logs() as logs:
        job_id = await queued(harness)
        await harness.run(job_id)

    lifecycle = [line for line in logs.for_field("job_id", str(job_id)) if line["message"] in LIFECYCLE_LINES]
    assert len(lifecycle) == 7
    assert {line["source_kind"] for line in lifecycle} == {"article"}
    assert {line["document_id"] for line in lifecycle} == {str(DOCUMENT_ID)}
    finished = {"outcome": "succeeded", "failure_code": ""}
    assert sample(harness, "deckly_generation_jobs_finished_total", **finished, source="article") == 1
    assert sample(harness, "deckly_generation_jobs_finished_total", **finished, source="document") is None
    assert (
        sample(harness, "deckly_generation_job_duration_seconds_count", outcome="succeeded", source="article")
        == 1
    )
    for stage in ARTICLE_STAGES:
        assert (
            sample(
                harness,
                "deckly_generation_stage_duration_seconds_count",
                stage=stage,
                outcome="ok",
                source="article",
            )
            == 1
        )


async def test_queued_article_job_cancelled_before_a_worker_takes_it_is_never_downloaded() -> None:
    harness = harness_with_article()
    job_id = await queued(harness)
    await harness.cancel(job_id)

    await harness.run(job_id)

    assert isinstance(harness.store.jobs[job_id].state, Cancelled)
    assert harness.queue.aborted == [job_id]
    assert harness.documents.storage.reads == []
    assert harness.documents.parsed == []


async def test_article_request_is_screened_at_admission_with_the_article_and_its_instructions() -> None:
    harness = harness_with_article()
    request = replace(document_request(), instructions="Only the warning signs")

    await queued(harness, request)

    assert harness.moderator.screened == [request]
    assert harness.moderator.documents == [UPLOADED_ARTICLE]


async def test_article_request_whose_title_or_instructions_break_the_policy_is_rejected_at_admission() -> (
    None
):
    harness = harness_with_article()
    harness.moderator.outcome = False

    with captured_json_logs() as logs, pytest.raises(TopicRejectedError):
        await queued(harness)

    assert harness.store.jobs == {}
    assert harness.queue.enqueued == []
    assert harness.quota.used_by_client[DOCUMENT_CLIENT_ID] == 1
    [rejected] = logs.named("topic_rejected")
    assert rejected["source_kind"] == "article"
    assert (
        sample(harness, "deckly_generation_jobs_admitted_total", outcome="topic_rejected", source="article")
        == 1
    )


async def test_whole_page_text_is_screened_in_the_worker_before_any_card_is_written() -> None:
    harness = harness_with_article()
    job_id = await queued(harness)

    await harness.run(job_id)

    assert harness.documents.screened == [(job_id, UPLOADED_ARTICLE, ARTICLE_MATERIAL)]


async def test_article_whose_page_text_breaks_the_policy_is_rejected_before_any_card_is_written() -> None:
    harness = harness_with_article()
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
    assert failed["source_kind"] == "article"
    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="failed",
            failure_code="DOCUMENT_REJECTED",
            source="article",
        )
        == 1
    )


async def test_article_cards_are_screened_under_the_article_title_and_blocked_ones_are_dropped() -> None:
    harness = harness_with_article()
    [unsafe, *kept] = GENERATED.notes
    harness.providers.unsafe_notes = {unsafe.client_id}
    job_id = await queued(harness)

    await harness.run(job_id)

    state = harness.store.jobs[job_id].state
    assert isinstance(state, Succeeded)
    assert state.result.notes == tuple(kept)
    assert harness.providers.subjects == [UPLOADED_ARTICLE.filename]


async def test_article_whose_generated_cards_are_all_blocked_fails_without_being_cached() -> None:
    harness = harness_with_article()
    harness.providers.unsafe_notes = {note.client_id for note in GENERATED.notes}
    job_id = await queued(harness)

    with captured_json_logs() as logs:
        await harness.run(job_id)

    failure = failure_of(harness, job_id)
    assert failure.code is FailureCode.NO_VALID_CONTENT
    assert failure.stage is JobStage.GENERATING_CARDS
    assert harness.cache.writes == []
    [failed] = logs.named("generation_failed")
    assert failed["source_kind"] == "article"


async def test_second_article_job_with_the_same_settings_is_served_from_the_document_cache() -> None:
    harness = harness_with_article()
    await harness.run(await queued(harness))
    second = await queued(harness, key=2)

    await harness.run(second)

    state = harness.store.jobs[second].state
    assert isinstance(state, Succeeded)
    assert state.result == GENERATED
    assert len(harness.documents.parsed) == 1
    assert len(harness.providers.generated_for) == 1
    [fingerprint] = harness.cache.entries
    assert isinstance(fingerprint, DocumentGenerationFingerprint)
    assert fingerprint == document_request().fingerprint(UPLOADED_ARTICLE)


async def test_article_cached_under_its_own_id_is_never_served_for_another_document() -> None:
    harness = harness_with_article()
    other = replace(UPLOADED_DOCUMENT, document_id=UUID("6e1d8b2f-4c3a-4d9e-8f7a-2b3c4d5e6f70"))
    harness.documents.keep(other)
    await harness.run(await queued(harness))

    await harness.run(await queued(harness, document_request(other.document_id), key=2))

    assert len(harness.providers.generated_for) == 2
    assert len(harness.cache.entries) == 2


async def test_replayed_article_request_is_counted_as_an_article() -> None:
    harness = harness_with_article()
    await queued(harness)

    await queued(harness)

    assert sample(harness, "deckly_generation_jobs_admitted_total", outcome="replayed", source="article") == 1


async def test_article_job_found_running_on_redelivery_is_abandoned_and_counted_as_an_article() -> None:
    harness = harness_with_article()
    job_id = await queued(harness)
    harness.store.replace(harness.store.jobs[job_id].start(harness.now))

    with captured_json_logs() as logs:
        await harness.run(job_id)

    assert failure_of(harness, job_id).code is FailureCode.GENERATION_FAILED
    [abandoned] = logs.named("generation_abandoned")
    assert abandoned["source_kind"] == "article"
    assert (
        sample(
            harness,
            "deckly_generation_jobs_finished_total",
            outcome="failed",
            failure_code="GENERATION_FAILED",
            source="article",
        )
        == 1
    )
