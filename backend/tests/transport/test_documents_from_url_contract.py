from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http import HTTPStatus

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.documents import FetchArticle
from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import DocumentUpload
from deckly.domain.article_url import MAX_ARTICLE_URL_LENGTH, ArticleUrl, parse_article_url
from deckly.domain.exceptions import (
    ArticleBlockedByRobotsError,
    ArticleHasNoTextError,
    ArticleUnavailableError,
    UnsupportedDocumentError,
)
from deckly.infrastructure.article_url.fetcher import (
    FETCH_RETRY_AFTER_SECONDS,
    NO_TEXT_MESSAGE,
    UNREACHABLE_MESSAGE,
    ArticleTextInspector,
)
from deckly.infrastructure.article_url.robots import RobotsUnreachableError
from deckly.infrastructure.observability.metrics import ARTICLE_URL_GATE
from deckly.infrastructure.upload_gate import RejectingUploadGate
from deckly.main import API_PREFIX
from deckly.transport import documents
from deckly.transport.error_handlers import register_error_handlers
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.documents import CLIENT_ID, OTHER_CLIENT_ID, DocumentHarness
from tests.transport.openapi import declared_responses, spec_errors

FROM_URL = f"{API_PREFIX}/documents/from-url"
DOCUMENTS = f"{API_PREFIX}/documents"
FETCH_SPEC = ("/documents/from-url", "post")
HEADERS = {"X-Client-Id": str(CLIENT_ID)}
LINK = "https://en.wikipedia.org/wiki/Spaced_repetition"
TEXT = "Spaced repetition is an evidence-based learning technique usually performed with flashcards."
TITLE = "Spaced repetition - Wikipedia"


@dataclass
class ScriptedArticles:
    upload: DocumentUpload = field(
        default_factory=lambda: DocumentUpload(filename=TITLE, content=TEXT.encode(), source_url=LINK)
    )
    failure: Exception | None = None
    fetched: list[ArticleUrl] = field(default_factory=list)

    async def fetch(self, article: ArticleUrl) -> DocumentUpload:
        self.fetched.append(article)
        if self.failure is not None:
            raise self.failure
        return self.upload


@dataclass
class Wired:
    harness: DocumentHarness
    fetcher: ScriptedArticles
    client: TestClient


def fetch_app(harness: DocumentHarness, fetcher: ScriptedArticles) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(documents.router, prefix=API_PREFIX)
    app.state.fetch_article = FetchArticle(
        upload=replace(
            harness.upload,
            gate=RejectingUploadGate(harness.slots, ARTICLE_URL_GATE, harness.metrics),
            inspector=ArticleTextInspector(),
        ),
        fetcher=fetcher,
    )
    app.state.get_document = harness.get
    return app


@pytest.fixture
def wired() -> Iterator[Wired]:
    harness = DocumentHarness()
    fetcher = ScriptedArticles()
    with TestClient(fetch_app(harness, fetcher), raise_server_exceptions=False) as client:
        yield Wired(harness, fetcher, client)


def post(client: TestClient, body: object, headers: dict[str, str] | None = None) -> httpx2.Response:
    return client.post(FROM_URL, json=body, headers=HEADERS if headers is None else headers)


def assert_problem(response: httpx2.Response, status: HTTPStatus, code: str) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    assert str(response.status_code) in declared_responses(*FETCH_SPEC)
    body: dict[str, object] = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == code
    return body


def test_page_is_201_with_a_text_document_that_names_its_source_and_reads_back(wired: Wired) -> None:
    response = post(wired.client, {"url": f"  {LINK}#History  "})

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert str(response.status_code) in declared_responses(*FETCH_SPEC)
    body = response.json()
    assert spec_errors("Document", body) == []
    assert body["contentType"] == "text/plain"
    assert body["filename"] == TITLE
    assert body["sourceUrl"] == LINK
    assert body["sizeBytes"] == len(TEXT.encode())
    assert "durationSeconds" not in body
    assert wired.fetcher.fetched == [parse_article_url(LINK)]
    read = wired.client.get(f"{DOCUMENTS}/{body['documentId']}", headers=HEADERS)
    assert read.status_code == HTTPStatus.OK
    assert read.json() == body
    other = wired.client.get(
        f"{DOCUMENTS}/{body['documentId']}", headers={"X-Client-Id": str(OTHER_CLIENT_ID)}
    )
    assert other.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "https://[fd00:ec2::254]/latest/meta-data/",
        "http://localhost:6379/",
        "https://2130706433/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "gopher://example.com/",
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "https://example.com:8080/",
    ],
)
def test_unsupported_link_is_422_before_anything_is_fetched_or_counted(wired: Wired, url: str) -> None:
    assert_problem(
        post(wired.client, {"url": url}), HTTPStatus.UNPROCESSABLE_ENTITY, "UNSUPPORTED_ARTICLE_URL"
    )

    assert wired.fetcher.fetched == []
    assert wired.harness.limiter.used[CLIENT_ID] == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": ""},
        {"url": 42},
        {"url": None},
        {"url": LINK, "extra": True},
        {"link": LINK},
        [LINK],
        LINK,
        {"url": "x" * (MAX_ARTICLE_URL_LENGTH + 1)},
    ],
)
def test_malformed_body_is_400(wired: Wired, body: object) -> None:
    assert_problem(post(wired.client, body), HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")

    assert wired.fetcher.fetched == []


def test_missing_client_id_is_400(wired: Wired) -> None:
    assert_problem(post(wired.client, {"url": LINK}, headers={}), HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (
            ArticleUnavailableError(UNREACHABLE_MESSAGE),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "ARTICLE_UNAVAILABLE",
        ),
        (ArticleBlockedByRobotsError("robots"), HTTPStatus.UNPROCESSABLE_ENTITY, "ARTICLE_BLOCKED_BY_ROBOTS"),
        (ArticleHasNoTextError(NO_TEXT_MESSAGE), HTTPStatus.UNPROCESSABLE_ENTITY, "ARTICLE_HAS_NO_TEXT"),
        (UnsupportedDocumentError("pdf"), HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "UNSUPPORTED_DOCUMENT_TYPE"),
    ],
)
def test_fetch_refusals_map_to_their_problem_and_spend_the_budget(
    wired: Wired, failure: Exception, status: HTTPStatus, code: str
) -> None:
    wired.fetcher.failure = failure

    assert_problem(post(wired.client, {"url": LINK}), status, code)

    assert wired.harness.store.rows == {}
    assert wired.harness.limiter.used[CLIENT_ID] == 1


def test_unreachable_page_does_not_say_why_it_was_unreachable(wired: Wired) -> None:
    failure = ArticleUnavailableError(UNREACHABLE_MESSAGE)
    failure.add_note("egress refusals: [address]")
    wired.fetcher.failure = failure

    body = assert_problem(
        post(wired.client, {"url": LINK}), HTTPStatus.UNPROCESSABLE_ENTITY, "ARTICLE_UNAVAILABLE"
    )

    assert "address" not in str(body)
    assert "detail" not in body


@pytest.mark.parametrize(
    "failure",
    [RobotsUnreachableError(FETCH_RETRY_AFTER_SECONDS), UpstreamUnavailableError(FETCH_RETRY_AFTER_SECONDS)],
    ids=["robots-unreachable", "upstream-failure"],
)
def test_fetch_that_could_not_complete_is_503_with_retry_after(wired: Wired, failure: Exception) -> None:
    wired.fetcher.failure = failure

    body = assert_problem(
        post(wired.client, {"url": LINK}), HTTPStatus.SERVICE_UNAVAILABLE, "UPSTREAM_UNAVAILABLE"
    )

    assert body["retryAfterSeconds"] == FETCH_RETRY_AFTER_SECONDS
