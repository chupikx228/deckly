import json
from collections.abc import Callable
from dataclasses import replace

import httpx2
import pytest

from deckly.infrastructure.resilience import TransientError
from deckly.infrastructure.search.client import (
    SearchEndpoint,
    SearchError,
    SearchHit,
    SearchQuery,
    SearchQuotaExhaustedError,
    SearchRejectedError,
    SearchResponseError,
    SearchUnavailableError,
)
from deckly.infrastructure.search.tavily_client import REDACTED, TavilySearchClient

pytestmark = pytest.mark.anyio

API_KEY = "tvly-test-key"
ENDPOINT = SearchEndpoint(base_url="https://api.tavily.test", api_key=API_KEY, timeout_seconds=5)
QUERY = SearchQuery(text="Road signs", language="ru", max_results=6)

type Handler = Callable[[httpx2.Request], httpx2.Response]


def result(
    title: object = "Road signs",
    url: object = "https://example.com/signs",
    content: object = "A short snippet.",
    raw_content: object = "The full page text.",
) -> dict[str, object]:
    return {"title": title, "url": url, "content": content, "raw_content": raw_content, "score": 0.9}


def answering(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda _: httpx2.Response(status, json=body, headers=headers)


def raising(error: Exception) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        raise error

    return handler


async def search_with(
    handler: Handler, query: SearchQuery = QUERY, endpoint: SearchEndpoint = ENDPOINT
) -> tuple[SearchHit, ...]:
    client = TavilySearchClient(endpoint, httpx2.MockTransport(handler))
    try:
        return await client.search(query)
    finally:
        await client.aclose()


async def test_request_asks_for_extracted_page_text_with_the_topic_language_and_result_cap() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"results": []})

    await search_with(handler)

    [request] = seen
    assert (request.method, request.url.path) == ("POST", "/search")
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {
        "query": "Road signs",
        "topic": "general",
        "search_depth": "basic",
        "max_results": 6,
        "include_raw_content": "text",
        "include_answer": False,
        "include_images": False,
        "language": "ru",
    }


async def test_language_is_left_out_when_there_is_no_hint() -> None:
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.content)
        return httpx2.Response(200, json={"results": []})

    await search_with(handler, replace(QUERY, language=None))

    assert "language" not in json.loads(seen[0])


async def test_each_result_becomes_a_hit_with_its_own_title_url_and_page_text() -> None:
    body = {"results": [result(), result(title="Other", url="https://example.org/", raw_content="Other")]}

    hits = await search_with(answering(body))

    assert hits == (
        SearchHit(title="Road signs", url="https://example.com/signs", content="The full page text."),
        SearchHit(title="Other", url="https://example.org/", content="Other"),
    )


@pytest.mark.parametrize("raw_content", [None, "", "  \n ", 42])
async def test_snippet_stands_in_when_the_page_text_is_missing_or_blank(raw_content: object) -> None:
    [hit] = await search_with(answering({"results": [result(raw_content=raw_content)]}))

    assert hit.content == "A short snippet."


async def test_result_with_neither_page_text_nor_snippet_keeps_empty_content_for_the_parser_to_drop() -> None:
    [hit] = await search_with(answering({"results": [result(raw_content=None, content=None)]}))

    assert hit.content == ""


MALFORMED_RESULTS: dict[str, object] = {
    "not an object": "https://example.com",
    "missing title": {"url": "https://example.com", "raw_content": "text"},
    "missing url": {"title": "Title", "raw_content": "text"},
    "title not a string": result(title=None),
    "url not a string": result(url=["https://example.com"]),
}


@pytest.mark.parametrize("malformed", MALFORMED_RESULTS.values(), ids=MALFORMED_RESULTS.keys())
async def test_malformed_result_is_skipped_without_losing_the_others(malformed: object) -> None:
    hits = await search_with(answering({"results": [malformed, result()]}))

    assert [hit.url for hit in hits] == ["https://example.com/signs"]


async def test_empty_results_are_an_empty_answer_not_an_error() -> None:
    assert await search_with(answering({"results": [], "query": "Road signs"})) == ()


MALFORMED_BODIES: dict[str, object] = {
    "no results key": {"query": "Road signs"},
    "results not a list": {"results": {"0": result()}},
    "top level list": [result()],
    "null": None,
}


@pytest.mark.parametrize("body", MALFORMED_BODIES.values(), ids=MALFORMED_BODIES.keys())
async def test_body_without_a_results_list_is_a_response_error(body: object) -> None:
    with pytest.raises(SearchResponseError):
        await search_with(answering(body))


@pytest.mark.parametrize("content", [b"", b"<html>gateway</html>", b"\xff\xfe\x00binary"])
async def test_body_that_is_not_json_is_a_response_error_and_not_retried(content: bytes) -> None:
    with pytest.raises(SearchResponseError) as raised:
        await search_with(lambda _: httpx2.Response(200, content=content))

    assert not isinstance(raised.value, TransientError)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
async def test_transient_status_is_unavailable(status: int) -> None:
    with pytest.raises(SearchUnavailableError):
        await search_with(answering({"detail": {"error": "busy"}}, status))


async def test_retry_after_from_the_provider_is_carried_on_the_error() -> None:
    with pytest.raises(SearchUnavailableError) as raised:
        await search_with(answering({}, 429, {"retry-after": "7"}))

    assert raised.value.retry_after_seconds == 7


@pytest.mark.parametrize("status", [432, 433])
async def test_exhausted_plan_or_spending_limit_is_a_quota_error_that_is_not_retried(status: int) -> None:
    with pytest.raises(SearchQuotaExhaustedError) as raised:
        await search_with(answering({"detail": {"error": "limit exceeded"}}, status))

    assert not isinstance(raised.value, TransientError)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_rejected_request_is_not_retried(status: int) -> None:
    with pytest.raises(SearchRejectedError) as raised:
        await search_with(answering({"detail": {"error": "bad request"}}, status))

    assert not isinstance(raised.value, TransientError)


async def test_error_detail_from_the_provider_is_kept_for_the_logs() -> None:
    with pytest.raises(SearchRejectedError, match="Invalid language code"):
        await search_with(answering({"detail": {"error": "Invalid language code"}}, 400))


async def test_api_key_echoed_back_by_the_provider_never_reaches_the_error() -> None:
    body = {"detail": {"error": f"Unauthorized: missing or invalid API key {API_KEY}"}}

    with pytest.raises(SearchRejectedError) as raised:
        await search_with(answering(body, 401))

    assert API_KEY not in str(raised.value)
    assert REDACTED in str(raised.value)


async def test_oversized_error_detail_is_truncated() -> None:
    with pytest.raises(SearchRejectedError) as raised:
        await search_with(answering({"detail": "x" * 10_000}, 400))

    assert len(str(raised.value)) < 1000


@pytest.mark.parametrize(
    "error",
    [
        httpx2.ConnectError("refused"),
        httpx2.ReadTimeout("slow"),
        httpx2.ConnectTimeout("slow"),
        httpx2.RemoteProtocolError("reset"),
    ],
    ids=["connect", "read timeout", "connect timeout", "protocol"],
)
async def test_network_failure_is_unavailable(error: Exception) -> None:
    with pytest.raises(SearchUnavailableError):
        await search_with(raising(error))


async def test_every_failure_is_a_search_error() -> None:
    for handler in (answering({}, 500), answering({}, 400), answering({}, 432), answering([], 200)):
        with pytest.raises(SearchError):
            await search_with(handler)
