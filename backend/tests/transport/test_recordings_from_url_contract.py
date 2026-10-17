import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http import HTTPStatus

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.documents import FetchRecording
from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import DocumentUpload
from deckly.domain.exceptions import DocumentTooLargeError, RecordingTooLongError, RecordingUnavailableError
from deckly.domain.recording_url import MAX_RECORDING_URL_LENGTH, YouTubeVideo
from deckly.infrastructure.observability.metrics import RECORDING_URL_GATE
from deckly.infrastructure.recording_url.fetcher import FETCH_RETRY_AFTER_SECONDS, RecordingBlockedError
from deckly.infrastructure.upload_gate import RejectingUploadGate
from deckly.main import API_PREFIX
from deckly.transport import documents
from deckly.transport.error_handlers import RETRY_AFTER_HEADER, register_error_handlers
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.documents import CLIENT_ID, DocumentHarness, recording_harness
from tests.recording_files import fixture, pcm_wave
from tests.transport.openapi import declared_responses, spec_errors

FROM_URL = f"{API_PREFIX}/recordings/from-url"
DOCUMENTS = f"{API_PREFIX}/documents"
FETCH_SPEC = ("/recordings/from-url", "post")
READ_SPEC = ("/documents/{documentId}", "get")
HEADERS = {"X-Client-Id": str(CLIENT_ID)}
LINK = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@dataclass
class ScriptedFetcher:
    upload: DocumentUpload = field(
        default_factory=lambda: DocumentUpload(
            filename="Me at the zoo.m4a", content=fixture("tone-fragmented.m4a")
        )
    )
    failure: Exception | None = None
    fetched: list[YouTubeVideo] = field(default_factory=list)

    async def fetch(self, video: YouTubeVideo) -> DocumentUpload:
        self.fetched.append(video)
        if self.failure is not None:
            raise self.failure
        return self.upload


@dataclass
class Wired:
    harness: DocumentHarness
    fetcher: ScriptedFetcher
    client: TestClient


def fetch_app(harness: DocumentHarness, fetcher: ScriptedFetcher) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(documents.router, prefix=API_PREFIX)
    app.state.fetch_recording = FetchRecording(
        upload=replace(
            harness.upload, gate=RejectingUploadGate(harness.slots, RECORDING_URL_GATE, harness.metrics)
        ),
        fetcher=fetcher,
    )
    app.state.get_document = harness.get
    return app


@pytest.fixture
def wired() -> Iterator[Wired]:
    harness = recording_harness(max_duration_seconds=3)
    fetcher = ScriptedFetcher()
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


def test_supported_link_is_201_with_the_same_body_as_an_upload_and_reads_back(wired: Wired) -> None:
    response = post(wired.client, {"url": f"  {LINK}&t=30s  "})

    assert response.status_code == HTTPStatus.CREATED, response.text
    assert str(response.status_code) in declared_responses(*FETCH_SPEC)
    body = response.json()
    assert spec_errors("Document", body) == []
    assert body["contentType"] == "audio/mp4"
    assert body["filename"] == "Me at the zoo.m4a"
    assert body["durationSeconds"] == pytest.approx(2.064)
    assert wired.fetcher.fetched == [YouTubeVideo("jNQXAC9IVRw")]
    read = wired.client.get(f"{DOCUMENTS}/{body['documentId']}", headers=HEADERS)
    assert read.status_code == HTTPStatus.OK
    assert read.json() == body


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/76979871",
        "https://www.youtube.com/@channel",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://localhost:6379/",
        "gopher://www.youtube.com/watch?v=jNQXAC9IVRw",
    ],
)
def test_unsupported_link_is_422_and_nothing_is_fetched_or_counted(wired: Wired, url: str) -> None:
    assert_problem(
        post(wired.client, {"url": url}), HTTPStatus.UNPROCESSABLE_ENTITY, "UNSUPPORTED_RECORDING_URL"
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
        {"url": "x" * (MAX_RECORDING_URL_LENGTH + 1)},
    ],
)
def test_malformed_body_is_400(wired: Wired, body: object) -> None:
    assert_problem(post(wired.client, body), HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")

    assert wired.fetcher.fetched == []


def test_body_that_is_not_json_is_400(wired: Wired) -> None:
    response = wired.client.post(
        FROM_URL, content=b"url=x", headers={**HEADERS, "content-type": "text/plain"}
    )

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")


def test_missing_client_id_is_400(wired: Wired) -> None:
    assert_problem(post(wired.client, {"url": LINK}, headers={}), HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (RecordingTooLongError("metadata"), HTTPStatus.UNPROCESSABLE_ENTITY, "RECORDING_TOO_LONG"),
        (RecordingUnavailableError("private"), HTTPStatus.UNPROCESSABLE_ENTITY, "RECORDING_UNAVAILABLE"),
        (DocumentTooLargeError("big"), HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "DOCUMENT_TOO_LARGE"),
    ],
)
def test_fetch_refusals_map_to_their_problem(
    wired: Wired, failure: Exception, status: HTTPStatus, code: str
) -> None:
    wired.fetcher.failure = failure

    assert_problem(post(wired.client, {"url": LINK}), status, code)

    assert wired.harness.store.rows == {}
    assert wired.harness.limiter.used[CLIENT_ID] == 1


@pytest.mark.parametrize(
    "failure",
    [RecordingBlockedError(FETCH_RETRY_AFTER_SECONDS), UpstreamUnavailableError(FETCH_RETRY_AFTER_SECONDS)],
    ids=["blocked-by-youtube", "upstream-failure"],
)
def test_fetch_that_could_not_complete_is_503_with_retry_after(wired: Wired, failure: Exception) -> None:
    wired.fetcher.failure = failure

    body = assert_problem(
        post(wired.client, {"url": LINK}), HTTPStatus.SERVICE_UNAVAILABLE, "UPSTREAM_UNAVAILABLE"
    )

    assert body["retryAfterSeconds"] == FETCH_RETRY_AFTER_SECONDS


def test_downloaded_file_over_the_cap_is_422_from_the_probe(wired: Wired) -> None:
    wired.fetcher.upload = DocumentUpload(filename="long.wav", content=pcm_wave(seconds=4))

    assert_problem(post(wired.client, {"url": LINK}), HTTPStatus.UNPROCESSABLE_ENTITY, "RECORDING_TOO_LONG")

    assert wired.harness.storage.objects == {}


def test_downloaded_file_that_is_no_recording_is_415(wired: Wired) -> None:
    wired.fetcher.upload = DocumentUpload(filename="page.m4a", content=b"<html>consent page</html>")

    assert_problem(
        post(wired.client, {"url": LINK}), HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "UNSUPPORTED_DOCUMENT_TYPE"
    )


def test_spent_budget_is_429_with_retry_after(wired: Wired) -> None:
    wired.harness.limiter.limit = 0

    response = post(wired.client, {"url": LINK})

    assert_problem(response, HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED")
    assert RETRY_AFTER_HEADER in response.headers
    assert wired.fetcher.fetched == []


def test_busy_fetch_slot_is_503_with_a_short_retry_after() -> None:
    harness = recording_harness(concurrency=1)
    fetcher = ScriptedFetcher()
    asyncio.run(harness.slots.acquire())

    with TestClient(fetch_app(harness, fetcher), raise_server_exceptions=False) as saturated:
        response = post(saturated, {"url": LINK})

    assert_problem(response, HTTPStatus.SERVICE_UNAVAILABLE, "UPSTREAM_UNAVAILABLE")
    assert response.headers[RETRY_AFTER_HEADER] == "5"
    assert fetcher.fetched == []
    assert harness.limiter.used[CLIENT_ID] == 0
