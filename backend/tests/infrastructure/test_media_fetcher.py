import asyncio
import json
import logging
from dataclasses import replace
from uuid import UUID

import httpx2
import pytest

from deckly.application.pipeline import RunGeneration
from deckly.application.ports import ImageQuery, NoteMedia
from deckly.domain.job import STAGE_ORDER, GenerationJob, Succeeded
from deckly.domain.media import MediaKind
from deckly.infrastructure.media.client import (
    ImageCandidate,
    MediaBlockedError,
    MediaRejectedError,
    MediaResponseError,
    MediaUnavailableError,
)
from deckly.infrastructure.media.fetcher import CommonsMediaFetcher, MediaLimits
from deckly.infrastructure.resilience import CircuitBreaker, CircuitState, ResilientCaller
from deckly.transport.results import GenerationResultBody
from tests.domain.builders import basic_note, result_with
from tests.fakes import (
    ADDRESS,
    MEDIA_LIMITS,
    MEDIA_POLICY,
    FakeImageSearchClient,
    Harness,
    ImageOutcome,
    ManualTime,
    commons_fetcher,
    commons_page,
    commons_results,
    generation_request,
    hang_forever,
    scope,
    sequential_job_ids,
)
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.anyio

FETCHER_LOGGER = "deckly.infrastructure.media.fetcher"
PIPELINE_LOGGER = "deckly.application.pipeline"
JOB = UUID(int=1, version=4)
WITH_IMAGES = replace(generation_request(), include_images=True)


def query(number: int, text: str | None = None) -> ImageQuery:
    return ImageQuery(client_id=UUID(int=number, version=4), text=text or f"picture {number}")


def candidate(title: str) -> ImageCandidate:
    return ImageCandidate(
        file_title=title,
        mime="image/png",
        thumbnail_url=f"https://upload.wikimedia.org/thumb/{title.removeprefix('File:')}",
        thumbnail_width=960,
        thumbnail_height=640,
        license_code="cc0",
        attribution_required="false",
        restrictions="",
        description=f"Picture of {title}",
    )


async def hang_search() -> tuple[ImageCandidate, ...]:
    await hang_forever()
    return ()


def unlicensed(title: str) -> ImageCandidate:
    return replace(candidate(title), license_code="cc-by-sa-4.0", attribution_required="true")


def fetcher_with(
    *outcomes: ImageOutcome,
    limits: MediaLimits = MEDIA_LIMITS,
    breaker: CircuitBreaker | None = None,
    time: ManualTime | None = None,
) -> tuple[CommonsMediaFetcher, FakeImageSearchClient]:
    clock = time or ManualTime()
    client = FakeImageSearchClient(*outcomes)
    fetcher = CommonsMediaFetcher(
        client=client,
        caller=ResilientCaller(MEDIA_POLICY, breaker or clock.breaker(), clock.runtime()),
        new_id=sequential_job_ids().__next__,
        limits=limits,
    )
    return fetcher, client


def titles(attachments: tuple[NoteMedia, ...]) -> list[tuple[UUID, str | None]]:
    return [(attachment.client_id, attachment.media.alt) for attachment in attachments]


async def test_each_note_gets_the_first_candidate_with_a_derivable_licence() -> None:
    fetcher, _ = fetcher_with(
        (unlicensed("File:A.png"), candidate("File:B.png"), candidate("File:C.png")),
        (candidate("File:D.png"),),
    )

    attachments = await fetcher.fetch(JOB, (query(1), query(2)))

    assert titles(attachments) == [
        (query(1).client_id, "Picture of File:B.png"),
        (query(2).client_id, "Picture of File:D.png"),
    ]
    assert all(attachment.media.license == "CC0-1.0" for attachment in attachments)
    assert all(attachment.media.kind is MediaKind.IMAGE for attachment in attachments)
    assert len({attachment.media.media_id for attachment in attachments}) == 2


async def test_note_whose_candidates_have_no_derivable_licence_gets_no_image_and_the_others_still_do() -> (
    None
):
    fetcher, _ = fetcher_with(
        (unlicensed("File:A.png"), replace(candidate("File:B.png"), restrictions="personality")),
        (candidate("File:C.png"),),
    )

    attachments = await fetcher.fetch(JOB, (query(1), query(2)))

    assert [attachment.client_id for attachment in attachments] == [query(2).client_id]


async def test_the_same_file_is_not_attached_to_two_notes() -> None:
    shared = candidate("File:Shared.png")
    fetcher, _ = fetcher_with((shared,), (shared, candidate("File:Other.png")), (shared,))

    attachments = await fetcher.fetch(JOB, (query(1), query(2), query(3)))

    assert titles(attachments) == [
        (query(1).client_id, "Picture of File:Shared.png"),
        (query(2).client_id, "Picture of File:Other.png"),
    ]


async def test_search_asks_for_the_configured_candidates_and_thumbnail_width() -> None:
    limits = replace(MEDIA_LIMITS, candidates_per_query=7, thumbnail_width=640)
    fetcher, client = fetcher_with((), limits=limits)

    await fetcher.fetch(JOB, (query(1, "stop sign"),))

    [search] = client.searches
    assert (search.text, search.max_candidates, search.thumbnail_width) == ("stop sign", 7, 640)


async def test_only_the_first_query_of_a_note_is_searched_and_only_up_to_the_image_cap() -> None:
    fetcher, client = fetcher_with((candidate("File:A.png"),), limits=replace(MEDIA_LIMITS, max_images=2))

    await fetcher.fetch(JOB, (query(1, "first"), query(1, "second"), query(2), query(3)))

    assert sorted(search.text for search in client.searches) == ["first", "picture 2"]


async def test_no_queries_means_no_search() -> None:
    fetcher, client = fetcher_with(())

    assert await fetcher.fetch(JOB, ()) == ()
    assert client.searches == []


async def test_transient_failure_is_retried_and_then_attaches() -> None:
    time = ManualTime()
    fetcher, client = fetcher_with(MediaUnavailableError("503"), (candidate("File:A.png"),), time=time)

    attachments = await fetcher.fetch(JOB, (query(1),))

    assert len(attachments) == 1
    assert len(client.searches) == 2
    assert time.sleeps == [MEDIA_POLICY.base_delay_seconds]


FAILED_SEARCHES: dict[str, tuple[Exception, int]] = {
    "outage": (MediaUnavailableError("503"), MEDIA_POLICY.max_attempts),
    "timeout": (TimeoutError(), MEDIA_POLICY.max_attempts),
    "blocked user agent": (MediaBlockedError("403"), 1),
    "rejected request": (MediaRejectedError("400"), 1),
    "malformed answer": (MediaResponseError("not json"), 1),
}


@pytest.mark.parametrize(("error", "attempts"), FAILED_SEARCHES.values(), ids=FAILED_SEARCHES.keys())
async def test_failed_search_leaves_its_note_without_an_image_and_never_raises(
    error: Exception, attempts: int
) -> None:
    fetcher, client = fetcher_with(error)

    assert await fetcher.fetch(JOB, (query(1),)) == ()
    assert len(client.searches) == attempts


async def test_one_failed_search_does_not_cost_the_other_notes_their_images() -> None:
    fetcher, _ = fetcher_with(
        (candidate("File:A.png"),),
        MediaRejectedError("400"),
        (candidate("File:C.png"),),
        limits=replace(MEDIA_LIMITS, max_concurrency=1),
    )

    attachments = await fetcher.fetch(JOB, (query(1), query(2), query(3)))

    assert [attachment.client_id for attachment in attachments] == [query(1).client_id, query(3).client_id]


async def test_open_circuit_means_no_images_without_calling_the_provider() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    breaker.record_failure()
    fetcher, client = fetcher_with((candidate("File:A.png"),), breaker=breaker, time=time)

    assert await fetcher.fetch(JOB, (query(1), query(2))) == ()
    assert client.searches == []


async def test_repeated_blocks_open_the_circuit_and_the_remaining_searches_fail_fast() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=2)
    fetcher, client = fetcher_with(
        MediaBlockedError("403"), breaker=breaker, time=time, limits=replace(MEDIA_LIMITS, max_concurrency=1)
    )

    assert await fetcher.fetch(JOB, tuple(query(number) for number in range(1, 6))) == ()
    assert len(client.searches) == 2
    assert breaker.state is CircuitState.OPEN


async def test_stage_deadline_keeps_the_images_found_in_time_and_abandons_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetcher, _ = fetcher_with(
        (candidate("File:A.png"),), hang_search, limits=replace(MEDIA_LIMITS, deadline_seconds=0.05)
    )

    with caplog.at_level(logging.INFO, logger=FETCHER_LOGGER):
        attachments = await fetcher.fetch(JOB, (query(1), query(2)))

    assert [attachment.client_id for attachment in attachments] == [query(1).client_id]
    [record] = [record for record in caplog.records if record.getMessage() == "media_fetched"]
    assert record.levelno == logging.WARNING
    assert record.__dict__["deadline_reached"] is True


async def test_abandoned_search_does_not_count_against_the_circuit() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=1)
    fetcher, _ = fetcher_with(
        hang_search, breaker=breaker, time=time, limits=replace(MEDIA_LIMITS, deadline_seconds=0.05)
    )

    await fetcher.fetch(JOB, (query(1),))

    assert breaker.state is CircuitState.CLOSED


async def test_searches_never_exceed_the_configured_concurrency() -> None:
    in_flight = 0
    peak = 0

    async def search() -> tuple[ImageCandidate, ...]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1
        return ()

    fetcher, client = fetcher_with(search, limits=replace(MEDIA_LIMITS, max_concurrency=2))

    await fetcher.fetch(JOB, tuple(query(number) for number in range(1, 8)))

    assert len(client.searches) == 7
    assert peak == 2


async def test_worker_cancellation_is_not_swallowed() -> None:
    fetcher, _ = fetcher_with(hang_search)
    running = asyncio.create_task(fetcher.fetch(JOB, (query(1),)))
    await asyncio.sleep(0.01)

    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running


async def test_bug_in_the_client_escapes_the_fetcher_for_the_pipeline_bulkhead_to_catch() -> None:
    fetcher, _ = fetcher_with(RuntimeError("client bug"))

    with pytest.raises(ExceptionGroup):
        await fetcher.fetch(JOB, (query(1),))


async def test_outcome_is_logged_with_counts_and_no_query_text(caplog: pytest.LogCaptureFixture) -> None:
    fetcher, _ = fetcher_with(
        (candidate("File:A.png"),),
        MediaUnavailableError("503"),
        limits=replace(MEDIA_LIMITS, max_concurrency=1),
    )

    with caplog.at_level(logging.INFO, logger=FETCHER_LOGGER):
        await fetcher.fetch(JOB, (query(1, "secret subject"), query(2)))

    [record] = [record for record in caplog.records if record.getMessage() == "media_fetched"]
    assert record.__dict__["job_id"] == str(JOB)
    assert (record.__dict__["searched"], record.__dict__["attached"]) == (2, 1)
    assert record.__dict__["failed_searches"] == {"UpstreamUnavailableError": 1}
    assert "secret subject" not in json.dumps(record.__dict__, default=str)


async def test_closing_the_fetcher_closes_the_image_client() -> None:
    fetcher, client = fetcher_with(())

    await fetcher.aclose()

    assert client.closed


async def run_with_commons(
    *,
    respond: object,
    breaker: CircuitBreaker | None = None,
) -> tuple[GenerationJob, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def recorded(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if isinstance(respond, Exception):
            raise respond
        if isinstance(respond, httpx2.Response):
            return respond
        return httpx2.Response(200, json=respond)

    harness = Harness()
    harness.providers.result = result_with(basic_note(1), basic_note(2))
    time = ManualTime()
    run = RunGeneration(
        store=harness.store,
        cache=harness.cache,
        retriever=harness.providers,
        parser=harness.providers,
        generator=harness.providers,
        moderator=harness.providers,
        media=commons_fetcher(recorded, time, breaker=breaker),
        clock=lambda: harness.now,
    )
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id
    await run(job_id)
    return await harness.get(job_id), seen


OUTAGES: dict[str, object] = {
    "server error": httpx2.Response(503),
    "rate limited": httpx2.Response(429, headers={"retry-after": "1"}),
    "blocked": httpx2.Response(403),
    "network timeout": httpx2.ReadTimeout("slow"),
    "connection refused": httpx2.ConnectError("refused"),
    "maintenance page": httpx2.Response(200, content=b"<html>down</html>"),
    "api error": httpx2.Response(200, json={"error": {"code": "readonly", "info": "maintenance"}}),
}


@pytest.mark.parametrize("respond", OUTAGES.values(), ids=OUTAGES.keys())
async def test_image_provider_outage_still_yields_a_succeeded_job_with_an_image_free_deck(
    respond: object,
) -> None:
    job, seen = await run_with_commons(respond=respond)

    assert isinstance(job.state, Succeeded)
    assert job.state.result == result_with(basic_note(1), basic_note(2))
    assert seen


async def test_open_image_circuit_still_yields_a_succeeded_job_without_calling_commons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30, clock=lambda: 0.0)
    breaker.record_failure()

    with caplog.at_level(logging.INFO, logger=PIPELINE_LOGGER):
        job, seen = await run_with_commons(respond=commons_results(commons_page(1)), breaker=breaker)

    assert isinstance(job.state, Succeeded)
    assert all(note.media == () for note in job.state.result.notes)
    assert seen == []
    assert not [record for record in caplog.records if record.getMessage() == "generation_failed"]


async def test_image_with_no_derivable_licence_never_reaches_the_result() -> None:
    pages = (
        commons_page(1, "File:By.png", license_code="cc-by-4.0", attribution_required="true"),
        commons_page(2, "File:Sa.png", license_code="cc-by-sa-3.0", attribution_required="true"),
        commons_page(3, "File:Unknown.png", license_code=None),
        commons_page(4, "File:Person.png", restrictions="personality"),
    )

    job, _ = await run_with_commons(respond=commons_results(*pages))

    assert isinstance(job.state, Succeeded)
    assert job.state.result == result_with(basic_note(1), basic_note(2))


async def test_licensed_images_are_attached_with_their_licence_alt_and_a_downloadable_url() -> None:
    page = commons_page(1, "File:Stop sign.svg", license_code="pd", description="<i>Stop</i> sign")

    job, seen = await run_with_commons(respond=commons_results(page, commons_page(2, "File:Yield.png")))

    assert isinstance(job.state, Succeeded)
    first, second = job.state.result.notes
    [image] = first.media
    assert (image.kind, image.license, image.alt, image.width, image.height) == (
        MediaKind.IMAGE,
        "Public-Domain",
        "Stop sign",
        960,
        960,
    )
    assert image.url.startswith("https://upload.wikimedia.org/")
    assert [item.alt for item in second.media] == ["A red octagonal stop sign"]
    assert len(seen) == 2
    body = GenerationResultBody.from_result(job.state.result).model_dump(mode="json", by_alias=True)
    assert spec_errors("GenerationResult", body) == []


async def test_bug_in_the_media_adapter_still_yields_a_succeeded_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()
    fetcher, _ = fetcher_with(RuntimeError("client bug"))
    run = RunGeneration(
        store=harness.store,
        cache=harness.cache,
        retriever=harness.providers,
        parser=harness.providers,
        generator=harness.providers,
        moderator=harness.providers,
        media=fetcher,
        clock=lambda: harness.now,
    )
    job_id = (await harness.create(WITH_IMAGES, scope(), ADDRESS)).job.job_id

    with caplog.at_level(logging.INFO, logger=PIPELINE_LOGGER):
        await run(job_id)

    job = await harness.get(job_id)
    assert isinstance(job.state, Succeeded)
    assert [entry.stage for entry in harness.store.history] == [*STAGE_ORDER, None]
    [record] = [record for record in caplog.records if record.getMessage() == "media_skipped"]
    assert record.levelno == logging.ERROR
