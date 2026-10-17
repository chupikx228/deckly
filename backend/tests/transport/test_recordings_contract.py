import asyncio
from collections.abc import Iterator
from http import HTTPStatus

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.main import API_PREFIX
from deckly.transport import documents
from deckly.transport.error_handlers import RETRY_AFTER_HEADER, register_error_handlers
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.documents import (
    CLIENT_ID,
    MAX_TEST_DOCUMENT_BYTES,
    DocumentHarness,
    document,
    pdf_bytes,
    recording_harness,
)
from tests.recording_files import fixture, pcm_wave
from tests.transport.openapi import declared_responses, spec_errors

RECORDINGS = f"{API_PREFIX}/recordings"
DOCUMENTS = f"{API_PREFIX}/documents"
UPLOAD_SPEC = ("/recordings", "post")
DOCUMENT_UPLOAD_SPEC = ("/documents", "post")
READ_SPEC = ("/documents/{documentId}", "get")
HEADERS = {"X-Client-Id": str(CLIENT_ID)}


def recordings_app(recordings: DocumentHarness, uploads: DocumentHarness) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(documents.router, prefix=API_PREFIX)
    app.state.upload_recording = recordings.upload
    app.state.upload_document = uploads.upload
    app.state.get_document = recordings.get
    return app


@pytest.fixture
def harness() -> DocumentHarness:
    return recording_harness(max_duration_seconds=3)


@pytest.fixture
def document_harness() -> DocumentHarness:
    return DocumentHarness()


@pytest.fixture
def client(harness: DocumentHarness, document_harness: DocumentHarness) -> Iterator[TestClient]:
    with TestClient(recordings_app(harness, document_harness), raise_server_exceptions=False) as test_client:
        yield test_client


def send(
    client: TestClient, content: bytes, filename: str = "memo.m4a", endpoint: str = RECORDINGS
) -> httpx2.Response:
    return client.post(endpoint, files={"file": (filename, content, "audio/mpeg")}, headers=HEADERS)


def assert_body(response: httpx2.Response, status: HTTPStatus, spec: tuple[str, str]) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert str(response.status_code) in declared_responses(*spec)
    body: dict[str, object] = response.json()
    assert spec_errors("Document", body) == []
    return body


def assert_problem(
    response: httpx2.Response, status: HTTPStatus, code: str, spec: tuple[str, str] = UPLOAD_SPEC
) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    assert str(response.status_code) in declared_responses(*spec)
    body: dict[str, object] = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == code
    return body


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("tone.mp3", "audio/mpeg"),
        ("tone.m4a", "audio/mp4"),
        ("tone.wav", "audio/wav"),
        ("clip.mp4", "video/mp4"),
        ("clip.mov", "video/quicktime"),
    ],
)
def test_uploaded_recording_is_201_with_its_detected_type_and_duration(
    client: TestClient, name: str, content_type: str
) -> None:
    content = fixture(name)

    body = assert_body(send(client, content, filename=name), HTTPStatus.CREATED, UPLOAD_SPEC)

    assert body["contentType"] == content_type
    assert body["sizeBytes"] == len(content)
    assert body["filename"] == name
    assert isinstance(body["durationSeconds"], float)
    assert 1 <= body["durationSeconds"] < 3


def test_uploaded_recording_reads_back_with_its_duration(client: TestClient) -> None:
    created = assert_body(send(client, fixture("tone.wav")), HTTPStatus.CREATED, UPLOAD_SPEC)

    read = assert_body(
        client.get(f"{DOCUMENTS}/{created['documentId']}", headers=HEADERS), HTTPStatus.OK, READ_SPEC
    )

    assert read == created
    assert read["durationSeconds"] == 1.0


def test_document_reads_back_without_a_duration(client: TestClient, harness: DocumentHarness) -> None:
    stored = document(1)
    harness.store.keep(stored)

    read = assert_body(
        client.get(f"{DOCUMENTS}/{stored.document_id}", headers=HEADERS), HTTPStatus.OK, READ_SPEC
    )

    assert "durationSeconds" not in read


def test_recording_over_the_duration_cap_is_422_and_never_stored(
    client: TestClient, harness: DocumentHarness
) -> None:
    body = assert_problem(
        send(client, pcm_wave(seconds=4), filename="long.wav"),
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "RECORDING_TOO_LONG",
    )

    assert body["status"] == HTTPStatus.UNPROCESSABLE_ENTITY
    assert harness.store.rows == {}
    assert harness.storage.objects == {}


@pytest.mark.parametrize(
    "content",
    [pdf_bytes(), fixture("silent.mp4"), fixture("red.heic"), b"not a recording at all"],
    ids=["pdf", "video-without-audio", "heic", "text"],
)
def test_file_that_is_no_supported_recording_is_415_and_never_stored(
    client: TestClient, harness: DocumentHarness, content: bytes
) -> None:
    assert_problem(
        send(client, content, filename="memo.mp3"),
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        "UNSUPPORTED_DOCUMENT_TYPE",
    )

    assert harness.store.rows == {}
    assert harness.storage.objects == {}


@pytest.mark.parametrize("name", ["tone.mp3", "tone.m4a", "tone.wav", "clip.mp4", "clip.mov"])
def test_recording_sent_to_the_document_endpoint_is_415(
    client: TestClient, document_harness: DocumentHarness, name: str
) -> None:
    assert_problem(
        send(client, fixture(name), filename="lecture.pdf", endpoint=DOCUMENTS),
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        "UNSUPPORTED_DOCUMENT_TYPE",
        DOCUMENT_UPLOAD_SPEC,
    )

    assert document_harness.store.rows == {}


def test_recording_over_the_size_limit_is_413_and_never_stored(
    client: TestClient, harness: DocumentHarness
) -> None:
    content = fixture("tone.wav") + b"\x00" * MAX_TEST_DOCUMENT_BYTES

    assert_problem(
        send(client, content, filename="big.wav"), HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "DOCUMENT_TOO_LARGE"
    )

    assert harness.store.rows == {}


def test_full_recording_gate_is_503_while_document_uploads_still_go_through() -> None:
    recordings = recording_harness(concurrency=1)
    uploads = DocumentHarness()
    asyncio.run(recordings.slots.acquire())

    with TestClient(recordings_app(recordings, uploads), raise_server_exceptions=False) as saturated:
        refused = send(saturated, fixture("tone.wav"))
        accepted = send(saturated, pdf_bytes(), filename="lecture.pdf", endpoint=DOCUMENTS)

    assert_problem(refused, HTTPStatus.SERVICE_UNAVAILABLE, "UPSTREAM_UNAVAILABLE")
    assert refused.headers[RETRY_AFTER_HEADER] == "5"
    assert recordings.reads == 0
    assert recordings.limiter.used[CLIENT_ID] == 0
    assert_body(accepted, HTTPStatus.CREATED, DOCUMENT_UPLOAD_SPEC)
    assert (
        recordings.metrics.registry.get_sample_value(
            "deckly_gate_rejections_total", {"gate": "recording_upload"}
        )
        == 1
    )
