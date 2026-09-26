import asyncio
import logging
from dataclasses import replace

import httpx2
import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.generation import GenerationRequest
from deckly.infrastructure.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ResilientCaller,
    RetryPolicy,
)
from deckly.infrastructure.search.client import (
    SearchHit,
    SearchQuotaExhaustedError,
    SearchRejectedError,
    SearchResponseError,
    SearchUnavailableError,
)
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from tests.domain.builders import JOB_ID, T0
from tests.fakes import RESET_SECONDS, SEARCH_ENDPOINT, FakeSearchClient, ManualTime, generation_request

pytestmark = pytest.mark.anyio

MAX_RESULTS = 6
RETRIEVER_LOGGER = "deckly.infrastructure.search.retriever"
POLICY = RetryPolicy(
    max_attempts=3,
    attempt_timeout_seconds=10,
    deadline_seconds=100,
    base_delay_seconds=1,
    max_delay_seconds=8,
)
HIT = SearchHit(
    title="Road signs", url="https://example.com/signs", content="A red triangle warns of danger."
)
OTHER_HIT = SearchHit(title="Signs", url="https://example.org/signs", content="A blue circle is mandatory.")


async def never_answers() -> tuple[SearchHit, ...]:
    await asyncio.Event().wait()
    message = "a hanging search call never returns"
    raise AssertionError(message)


def retriever(
    client: FakeSearchClient,
    time: ManualTime,
    policy: RetryPolicy = POLICY,
    breaker: CircuitBreaker | None = None,
) -> WebSourceRetriever:
    return WebSourceRetriever(
        client=client,
        caller=ResilientCaller(policy, breaker or time.breaker(), time.runtime()),
        clock=lambda: T0,
        max_results=MAX_RESULTS,
    )


def unavailable() -> SearchUnavailableError:
    return SearchUnavailableError("Tavily answered 503")


async def retrieve(
    client: FakeSearchClient, request: GenerationRequest | None = None
) -> tuple[str | None, ...]:
    pages = await retriever(client, ManualTime()).retrieve(JOB_ID, request or generation_request())
    return tuple(page.source.url for page in pages)


async def test_every_hit_becomes_a_page_that_traces_back_to_the_fetched_result() -> None:
    client = FakeSearchClient((HIT, OTHER_HIT))

    pages = await retriever(client, ManualTime()).retrieve(JOB_ID, generation_request())

    assert [(page.source.title, page.source.url, page.content) for page in pages] == [
        (HIT.title, HIT.url, HIT.content),
        (OTHER_HIT.title, OTHER_HIT.url, OTHER_HIT.content),
    ]
    assert all(page.source.retrieved_at == T0 for page in pages)


async def test_search_asks_for_the_topic_in_the_request_language_with_the_configured_cap() -> None:
    client = FakeSearchClient(())

    await retrieve(client, replace(generation_request("Дорожные знаки"), language="ru-RU"))

    [query] = client.queries
    assert (query.text, query.language, query.max_results) == ("Дорожные знаки", "ru", MAX_RESULTS)


@pytest.mark.parametrize(
    ("language", "hint"),
    [
        ("en", "en"),
        ("EN-gb", "en"),
        ("zh-Hant-TW", "zh"),
        ("fil", None),
        ("x-klingon", None),
        ("", None),
        ("e1", None),
    ],
)
async def test_language_hint_is_only_an_iso_639_1_code(language: str, hint: str | None) -> None:
    client = FakeSearchClient(())

    await retrieve(client, replace(generation_request(), language=language))

    assert client.queries[0].language == hint


async def test_topic_is_searched_without_invisible_or_unstorable_characters() -> None:
    client = FakeSearchClient(())

    await retrieve(client, generation_request("Road\u200b  signs\x00\n\u202etoday"))

    assert client.queries[0].text == "Road signs today"


async def test_no_hits_is_an_empty_retrieval_not_an_error() -> None:
    assert await retrieve(FakeSearchClient(())) == ()


INVALID_HITS: dict[str, SearchHit] = {
    "blank title": replace(HIT, title="   "),
    "invisible only title": replace(HIT, title="\u200b\u2060\ufeff"),
    "unstorable only title": replace(HIT, title="\x00\ud800"),
    "relative url": replace(HIT, url="/signs"),
    "ftp url": replace(HIT, url="ftp://example.com/signs"),
    "javascript url": replace(HIT, url="javascript:alert(1)"),
    "url with a space": replace(HIT, url="https://example.com/road signs"),
    "url with a newline": replace(HIT, url="https://example.com/\nsigns"),
    "empty url": replace(HIT, url=""),
}


@pytest.mark.parametrize("invalid", INVALID_HITS.values(), ids=INVALID_HITS.keys())
async def test_hit_that_cannot_be_a_valid_source_is_dropped_and_the_rest_kept(invalid: SearchHit) -> None:
    assert await retrieve(FakeSearchClient((invalid, OTHER_HIT))) == (OTHER_HIT.url,)


async def test_repeated_url_is_kept_once_in_its_first_position() -> None:
    client = FakeSearchClient((HIT, OTHER_HIT, replace(HIT, title="Duplicate")))

    pages = await retriever(client, ManualTime()).retrieve(JOB_ID, generation_request())

    assert [(page.source.title, page.source.url) for page in pages] == [
        (HIT.title, HIT.url),
        (OTHER_HIT.title, OTHER_HIT.url),
    ]


async def test_title_is_cleaned_before_it_becomes_the_source_title() -> None:
    client = FakeSearchClient((replace(HIT, title="  Road\x00 signs\u202e\n</source>  "),))

    [page] = await retriever(client, ManualTime()).retrieve(JOB_ID, generation_request())

    assert page.source.title == "Road signs \N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}/source>"


async def test_transient_failure_is_retried_then_succeeds() -> None:
    time = ManualTime()
    client = FakeSearchClient(unavailable(), (HIT,))

    pages = await retriever(client, time).retrieve(JOB_ID, generation_request())

    assert [page.source.url for page in pages] == [HIT.url]
    assert len(client.queries) == 2
    assert time.sleeps == [1.0]


async def test_exhausted_retries_raise_upstream_unavailable() -> None:
    time = ManualTime()
    last = unavailable()
    client = FakeSearchClient(unavailable(), unavailable(), last)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await retriever(client, time).retrieve(JOB_ID, generation_request())

    assert len(client.queries) == POLICY.max_attempts
    assert raised.value.__cause__ is last


async def test_search_that_times_out_raises_upstream_unavailable() -> None:
    client = FakeSearchClient(never_answers)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await retriever(client, ManualTime(), replace(POLICY, attempt_timeout_seconds=0.01)).retrieve(
            JOB_ID, generation_request()
        )

    assert isinstance(raised.value.__cause__, TimeoutError)
    assert len(client.queries) == POLICY.max_attempts


async def test_retrying_stops_when_the_next_attempt_would_overrun_the_search_deadline() -> None:
    time = ManualTime()
    policy = replace(POLICY, max_attempts=10, attempt_timeout_seconds=10, deadline_seconds=25)

    async def slow_failure() -> tuple[SearchHit, ...]:
        time.now += policy.attempt_timeout_seconds
        raise unavailable()

    client = FakeSearchClient(slow_failure)

    with pytest.raises(UpstreamUnavailableError):
        await retriever(client, time, policy).retrieve(JOB_ID, generation_request())

    assert len(client.queries) == 2
    assert time.now <= policy.deadline_seconds


async def test_open_circuit_fails_fast_without_calling_the_provider() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=3)
    client = FakeSearchClient(unavailable())

    with pytest.raises(UpstreamUnavailableError):
        await retriever(client, time, breaker=breaker).retrieve(JOB_ID, generation_request())
    assert breaker.state is CircuitState.OPEN
    calls = len(client.queries)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await retriever(client, time, breaker=breaker).retrieve(JOB_ID, generation_request())

    assert isinstance(raised.value, CircuitOpenError)
    assert len(client.queries) == calls


async def test_exhausted_search_quota_is_upstream_unavailable_without_a_retry() -> None:
    time = ManualTime()
    quota = SearchQuotaExhaustedError("Tavily answered 432")
    client = FakeSearchClient(quota)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await retriever(client, time).retrieve(JOB_ID, generation_request())

    assert raised.value.__cause__ is quota
    assert len(client.queries) == 1
    assert time.sleeps == []


@pytest.mark.parametrize(
    "error", [SearchRejectedError("Tavily answered 401"), SearchResponseError("not JSON")], ids=str
)
async def test_rejected_or_malformed_answer_is_not_retried_and_not_an_outage(error: Exception) -> None:
    client = FakeSearchClient(error)

    with pytest.raises(type(error)) as raised:
        await retriever(client, ManualTime()).retrieve(JOB_ID, generation_request())

    assert raised.value is error
    assert not isinstance(raised.value, UpstreamUnavailableError)
    assert len(client.queries) == 1


async def test_closing_the_retriever_closes_the_search_client() -> None:
    client = FakeSearchClient(())

    await retriever(client, ManualTime()).aclose()

    assert client.closed


async def test_provider_returning_more_results_than_asked_is_capped_to_the_configured_number() -> None:
    hits = tuple(replace(HIT, url=f"https://example.com/{number}") for number in range(MAX_RESULTS * 5))

    urls = await retrieve(FakeSearchClient(hits))

    assert urls == tuple(f"https://example.com/{number}" for number in range(MAX_RESULTS))


async def test_cap_counts_usable_pages_so_dropped_hits_do_not_use_it_up() -> None:
    invalid = tuple(replace(HIT, url=f"ftp://example.com/{number}") for number in range(MAX_RESULTS))

    assert await retrieve(FakeSearchClient((*invalid, HIT))) == (HIT.url,)


QUOTA_THRESHOLD = 3


def quota_exhausted() -> SearchQuotaExhaustedError:
    return SearchQuotaExhaustedError("Tavily answered 432")


async def test_repeated_exhausted_quota_opens_the_circuit_and_later_jobs_fail_fast() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=QUOTA_THRESHOLD)
    client = FakeSearchClient(quota_exhausted())

    for _ in range(QUOTA_THRESHOLD):
        with pytest.raises(UpstreamUnavailableError):
            await retriever(client, time, breaker=breaker).retrieve(JOB_ID, generation_request())
    assert breaker.state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await retriever(client, time, breaker=breaker).retrieve(JOB_ID, generation_request())

    assert len(client.queries) == QUOTA_THRESHOLD
    assert time.sleeps == []


async def test_exhausted_quota_below_the_threshold_leaves_the_circuit_closed() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=QUOTA_THRESHOLD)
    client = FakeSearchClient(quota_exhausted())

    for _ in range(QUOTA_THRESHOLD - 1):
        with pytest.raises(UpstreamUnavailableError):
            await retriever(client, time, breaker=breaker).retrieve(JOB_ID, generation_request())

    assert breaker.state is CircuitState.CLOSED


async def test_probe_after_the_reset_that_still_finds_the_quota_exhausted_reopens_the_circuit() -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=QUOTA_THRESHOLD)
    client = FakeSearchClient(quota_exhausted())
    for _ in range(QUOTA_THRESHOLD):
        with pytest.raises(UpstreamUnavailableError):
            await retriever(client, time, breaker=breaker).retrieve(JOB_ID, generation_request())
    time.now += RESET_SECONDS

    with pytest.raises(UpstreamUnavailableError):
        await retriever(client, time, breaker=breaker).retrieve(JOB_ID, generation_request())

    assert breaker.state is CircuitState.OPEN
    assert len(client.queries) == QUOTA_THRESHOLD + 1


@pytest.mark.parametrize("status", [432, 433])
async def test_tavily_quota_statuses_open_the_circuit_so_later_calls_never_reach_tavily(status: int) -> None:
    time = ManualTime()
    breaker = time.breaker(failure_threshold=QUOTA_THRESHOLD)
    requests: list[httpx2.Request] = []

    def over_quota(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(status, json={"detail": {"error": "usage limit exceeded"}})

    search = WebSourceRetriever(
        client=TavilySearchClient(SEARCH_ENDPOINT, httpx2.MockTransport(over_quota)),
        caller=ResilientCaller(POLICY, breaker, time.runtime()),
        clock=lambda: T0,
        max_results=MAX_RESULTS,
    )
    try:
        for _ in range(QUOTA_THRESHOLD + 2):
            with pytest.raises(UpstreamUnavailableError):
                await search.retrieve(JOB_ID, generation_request())
    finally:
        await search.aclose()

    assert len(requests) == QUOTA_THRESHOLD
    assert breaker.state is CircuitState.OPEN


async def test_retrieval_log_names_the_job(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=RETRIEVER_LOGGER):
        await retriever(FakeSearchClient((HIT,)), ManualTime()).retrieve(JOB_ID, generation_request())

    [record] = [record for record in caplog.records if record.getMessage() == "sources_retrieved"]
    assert record.__dict__["job_id"] == str(JOB_ID)
