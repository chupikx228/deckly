from collections.abc import Callable

import httpx2
import pytest

from deckly.infrastructure.media.client import (
    ImageCandidate,
    ImageSearch,
    MediaBlockedError,
    MediaError,
    MediaRejectedError,
    MediaResponseError,
    MediaUnavailableError,
)
from deckly.infrastructure.media.commons_client import (
    MAX_SEARCH_CHARACTERS,
    CommonsImageSearchClient,
    search_terms,
)
from deckly.infrastructure.resilience import PersistentError, TransientError
from tests.fakes import MEDIA_ENDPOINT, commons_page, commons_results

pytestmark = pytest.mark.anyio

SEARCH = ImageSearch(text="stop sign", max_candidates=10, thumbnail_width=960)

type Handler = Callable[[httpx2.Request], httpx2.Response]


def answering(body: object, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda _: httpx2.Response(status, json=body, headers=headers)


def raising(error: Exception) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        raise error

    return handler


async def search_with(handler: Handler, search: ImageSearch = SEARCH) -> tuple[ImageCandidate, ...]:
    client = CommonsImageSearchClient(MEDIA_ENDPOINT, httpx2.MockTransport(handler))
    try:
        return await client.search(search)
    finally:
        await client.aclose()


async def test_request_searches_files_with_licence_metadata_and_the_configured_limits() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=commons_results())

    await search_with(handler)

    [request] = seen
    assert (request.method, request.url.host, request.url.path) == (
        "GET",
        "commons.wikimedia.test",
        "/w/api.php",
    )
    assert request.headers["user-agent"] == MEDIA_ENDPOINT.user_agent
    assert dict(request.url.params) == {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": "stop sign filetype:bitmap|drawing",
        "gsrnamespace": "6",
        "gsrlimit": "10",
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "960",
        "iiextmetadatafilter": "License|AttributionRequired|Restrictions|ImageDescription",
    }


SEARCH_SYNTAX: dict[str, tuple[str, str]] = {
    "keyword filter": ("insource:/.*/ stop sign", "insource stop sign"),
    "negation and phrase": ('-"yield" stop', "yield stop"),
    "boolean operators": ("stop AND NOT sign OR yield", "stop and not sign or yield"),
    "wildcards and fuzziness": ("sto* sign~2 ?", "sto sign 2"),
    "file type override": ("stop filetype:video", "stop filetype video"),
    "non-latin letters and marks": ("знак «Стоп»", "знак стоп"),
    "devanagari with vowel signs": ("रुको चिह्न", "रुको चिह्न"),
}


@pytest.mark.parametrize(("text", "terms"), SEARCH_SYNTAX.values(), ids=SEARCH_SYNTAX.keys())
def test_model_query_is_reduced_to_plain_lowercase_words_so_it_cannot_steer_the_search(
    text: str, terms: str
) -> None:
    assert search_terms(text) == terms


def test_query_longer_than_commons_accepts_is_cut_at_a_word_to_fit() -> None:
    terms = search_terms("sign " * 100)

    assert len(terms) <= MAX_SEARCH_CHARACTERS
    assert terms.split() == ["sign"] * len(terms.split())


def test_single_word_longer_than_commons_accepts_is_cut_to_fit() -> None:
    assert len(search_terms("a" * 1000)) == MAX_SEARCH_CHARACTERS


@pytest.mark.parametrize("text", ["", "   ", "***", ":/-~", "\u200b\u2060"])
async def test_query_with_no_searchable_word_is_an_empty_answer_without_a_request(text: str) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=commons_results())

    candidates = await search_with(handler, ImageSearch(text=text, max_candidates=10, thumbnail_width=960))

    assert candidates == ()
    assert seen == []


async def test_each_page_becomes_a_candidate_with_its_thumbnail_and_licence_metadata() -> None:
    page = commons_page(1, "File:Stop sign.svg", description="<b>Stop</b> sign")

    [candidate] = await search_with(answering(commons_results(page)))

    assert candidate == ImageCandidate(
        file_title="File:Stop sign.svg",
        mime="image/svg+xml",
        thumbnail_url="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Stop_sign.svg/960px-Stop_sign.svg.png",
        thumbnail_width=960,
        thumbnail_height=960,
        license_code="cc0",
        attribution_required="false",
        restrictions="",
        description="<b>Stop</b> sign",
    )


async def test_candidates_come_back_in_search_rank_order_not_in_page_order() -> None:
    pages = [commons_page(3, "File:C.png"), commons_page(1, "File:A.png"), commons_page(2, "File:B.png")]

    candidates = await search_with(answering(commons_results(*pages)))

    assert [candidate.file_title for candidate in candidates] == ["File:A.png", "File:B.png", "File:C.png"]


async def test_missing_metadata_is_absent_rather_than_guessed() -> None:
    page = commons_page(
        1,
        info={"thumbwidth": "960"},
        license_code=None,
        attribution_required=None,
        restrictions=None,
        description=None,
    )

    [candidate] = await search_with(answering(commons_results(page)))

    assert (candidate.license_code, candidate.attribution_required, candidate.restrictions) == (
        None,
        None,
        None,
    )
    assert (candidate.description, candidate.thumbnail_width) == (None, None)


@pytest.mark.parametrize("value", [0, -5, True, 9.5, None])
async def test_dimension_that_is_not_a_positive_integer_is_absent(value: object) -> None:
    page = commons_page(1, info={"thumbwidth": value, "thumbheight": value})

    [candidate] = await search_with(answering(commons_results(page)))

    assert (candidate.thumbnail_width, candidate.thumbnail_height) == (None, None)


async def test_metadata_value_of_the_wrong_shape_is_absent() -> None:
    page = commons_page(1)
    page["imageinfo"] = [{"mime": "image/png", "extmetadata": {"License": "cc0", "AttributionRequired": {}}}]

    [candidate] = await search_with(answering(commons_results(page)))

    assert (candidate.license_code, candidate.attribution_required, candidate.thumbnail_url) == (
        None,
        None,
        None,
    )


MALFORMED_PAGES: dict[str, object] = {
    "not an object": "File:Stop.png",
    "no title": {"index": 2, "imageinfo": [{}]},
    "no rank": {"title": "File:Stop.png", "imageinfo": [{}]},
    "no image info": {"title": "File:Stop.png", "index": 2},
    "empty image info": {"title": "File:Stop.png", "index": 2, "imageinfo": []},
    "image info not an object": {"title": "File:Stop.png", "index": 2, "imageinfo": ["x"]},
}


@pytest.mark.parametrize("malformed", MALFORMED_PAGES.values(), ids=MALFORMED_PAGES.keys())
async def test_malformed_page_is_skipped_without_losing_the_others(malformed: object) -> None:
    body = {"batchcomplete": True, "query": {"pages": [malformed, commons_page(1, "File:Kept.png")]}}

    candidates = await search_with(answering(body))

    assert [candidate.file_title for candidate in candidates] == ["File:Kept.png"]


async def test_search_without_hits_is_an_empty_answer_not_an_error() -> None:
    assert await search_with(answering(commons_results())) == ()


UNEXPECTED_BODIES: dict[str, object] = {
    "list": [],
    "empty object": {},
    "query without pages": {"batchcomplete": True, "query": {}},
    "pages not a list": {"batchcomplete": True, "query": {"pages": {}}},
    "incomplete batch": {"batchcomplete": False},
}


@pytest.mark.parametrize("body", UNEXPECTED_BODIES.values(), ids=UNEXPECTED_BODIES.keys())
async def test_body_without_a_page_list_is_a_response_error(body: object) -> None:
    with pytest.raises(MediaResponseError):
        await search_with(answering(body))


@pytest.mark.parametrize("content", [b"<html>maintenance</html>", b"", b"{"])
async def test_body_that_is_not_json_is_a_response_error(content: bytes) -> None:
    with pytest.raises(MediaResponseError):
        await search_with(lambda _: httpx2.Response(200, content=content))


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
async def test_transient_status_is_unavailable(status: int) -> None:
    with pytest.raises(MediaUnavailableError):
        await search_with(answering({}, status))


async def test_retry_after_from_the_provider_is_carried_on_the_error() -> None:
    with pytest.raises(MediaUnavailableError) as error:
        await search_with(answering({}, 429, {"retry-after": "7"}))

    assert error.value.retry_after_seconds == 7


async def test_forbidden_is_a_persistent_block_that_is_not_retried_but_trips_the_breaker() -> None:
    with pytest.raises(MediaBlockedError) as error:
        await search_with(answering({}, 403))

    assert isinstance(error.value, PersistentError)
    assert not isinstance(error.value, TransientError)


@pytest.mark.parametrize("status", [301, 400, 401, 404, 414])
async def test_rejected_request_is_not_retried(status: int) -> None:
    with pytest.raises(MediaRejectedError):
        await search_with(answering({}, status))


@pytest.mark.parametrize("code", ["maxlag", "ratelimited", "readonly", "internal_api_error_DBQueryError"])
async def test_api_error_the_wiki_recovers_from_is_unavailable(code: str) -> None:
    body = {"error": {"code": code, "info": "Waiting for a database server"}}

    with pytest.raises(MediaUnavailableError) as error:
        await search_with(answering(body, 200, {"retry-after": "5"}))

    assert error.value.retry_after_seconds == 5


@pytest.mark.parametrize("code", ["badinteger", "unknown_action", "toomanyvalues"])
async def test_api_error_caused_by_the_request_is_rejected(code: str) -> None:
    body = {"error": {"code": code, "info": "x" * 2000}}

    with pytest.raises(MediaRejectedError) as error:
        await search_with(answering(body))

    assert len(str(error.value)) < 700


@pytest.mark.parametrize(
    "error", [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow"), httpx2.RemoteProtocolError("reset")]
)
async def test_network_failure_is_unavailable(error: Exception) -> None:
    with pytest.raises(MediaUnavailableError):
        await search_with(raising(error))


async def test_every_failure_is_a_media_error() -> None:
    for error_type in (MediaUnavailableError, MediaBlockedError, MediaRejectedError, MediaResponseError):
        assert issubclass(error_type, MediaError)
