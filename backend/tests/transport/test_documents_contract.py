import logging
from collections.abc import Iterator, MutableMapping
from http import HTTPStatus

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.main import API_PREFIX
from deckly.transport import documents
from deckly.transport.documents import MULTIPART_OVERHEAD_BYTES, body_limit
from deckly.transport.error_handlers import RETRY_AFTER_HEADER, register_error_handlers
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.documents import (
    CLIENT_ID,
    MAX_TEST_DOCUMENT_BYTES,
    OTHER_CLIENT_ID,
    UPLOAD_RETRY_AFTER_SECONDS,
    DocumentHarness,
    padded_pdf,
    pdf_bytes,
    pptx_bytes,
    storage_outage,
)
from tests.transport.openapi import declared_responses, spec_errors

ENDPOINT = f"{API_PREFIX}/documents"
UPLOAD_SPEC = ("/documents", "post")
READ_SPEC = ("/documents/{documentId}", "get")
HEADERS = {"X-Client-Id": str(CLIENT_ID)}
PDF_TYPE = "application/pdf"
PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
def harness() -> DocumentHarness:
    return DocumentHarness()


def documents_app(harness: DocumentHarness) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(documents.router, prefix=API_PREFIX)
    app.state.upload_document = harness.upload
    app.state.get_document = harness.get
    return app


@pytest.fixture
def client(harness: DocumentHarness) -> Iterator[TestClient]:
    with TestClient(documents_app(harness), raise_server_exceptions=False) as test_client:
        yield test_client


def upload(
    client: TestClient,
    content: bytes,
    filename: str = "lecture.pdf",
    declared_type: str = "application/octet-stream",
) -> httpx2.Response:
    return client.post(ENDPOINT, files={"file": (filename, content, declared_type)}, headers=HEADERS)


def assert_declared(response: httpx2.Response, spec: tuple[str, str]) -> None:
    assert str(response.status_code) in declared_responses(*spec), response.text


def assert_document(
    response: httpx2.Response, status: HTTPStatus, spec: tuple[str, str]
) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert_declared(response, spec)
    body: dict[str, object] = response.json()
    assert spec_errors("Document", body) == []
    return body


def assert_problem(
    response: httpx2.Response, status: HTTPStatus, code: str, spec: tuple[str, str] = UPLOAD_SPEC
) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    assert_declared(response, spec)
    body: dict[str, object] = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == code
    return body


def assert_nothing_stored(harness: DocumentHarness) -> None:
    assert harness.store.rows == {}
    assert harness.storage.objects == {}


def test_uploaded_pdf_is_201_with_its_detected_type(client: TestClient, harness: DocumentHarness) -> None:
    content = pdf_bytes()

    body = assert_document(upload(client, content), HTTPStatus.CREATED, UPLOAD_SPEC)

    assert body["contentType"] == PDF_TYPE
    assert body["sizeBytes"] == len(content)
    assert body["filename"] == "lecture.pdf"
    assert body["uploadedAt"] == "2026-08-14T10:30:00Z"
    assert len(harness.storage.objects) == 1


def test_pptx_declared_as_pdf_is_stored_as_what_it_really_is(client: TestClient) -> None:
    body = assert_document(
        upload(client, pptx_bytes(), filename="slides.pdf", declared_type=PDF_TYPE),
        HTTPStatus.CREATED,
        UPLOAD_SPEC,
    )

    assert body["contentType"] == PPTX_TYPE


def test_uploaded_document_reads_back_for_its_own_client(client: TestClient) -> None:
    created = assert_document(upload(client, pdf_bytes()), HTTPStatus.CREATED, UPLOAD_SPEC)

    read = assert_document(
        client.get(f"{ENDPOINT}/{created['documentId']}", headers=HEADERS), HTTPStatus.OK, READ_SPEC
    )

    assert read == created


def test_another_client_cannot_read_the_document(client: TestClient) -> None:
    created = assert_document(upload(client, pdf_bytes()), HTTPStatus.CREATED, UPLOAD_SPEC)

    response = client.get(
        f"{ENDPOINT}/{created['documentId']}", headers={"X-Client-Id": str(OTHER_CLIENT_ID)}
    )

    assert_problem(response, HTTPStatus.NOT_FOUND, "DOCUMENT_NOT_FOUND", READ_SPEC)


@pytest.mark.parametrize("document_id", ["0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21", "not-a-uuid", "1"])
def test_unknown_or_malformed_document_id_is_404(client: TestClient, document_id: str) -> None:
    response = client.get(f"{ENDPOINT}/{document_id}", headers=HEADERS)

    assert_problem(response, HTTPStatus.NOT_FOUND, "DOCUMENT_NOT_FOUND", READ_SPEC)


@pytest.mark.parametrize(
    "headers", [{}, {"X-Client-Id": "not-a-uuid"}, {"X-Client-Id": str(CLIENT_ID).upper()}]
)
def test_upload_without_a_valid_client_id_is_400_and_reads_nothing(
    client: TestClient, harness: DocumentHarness, headers: dict[str, str]
) -> None:
    response = client.post(ENDPOINT, files={"file": ("lecture.pdf", pdf_bytes())}, headers=headers)

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")

    assert harness.reads == 0
    assert_nothing_stored(harness)


def test_read_without_a_client_id_is_400(client: TestClient) -> None:
    response = client.get(f"{ENDPOINT}/0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21")

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED", READ_SPEC)


def test_file_in_a_field_with_another_name_is_400(client: TestClient, harness: DocumentHarness) -> None:
    response = client.post(ENDPOINT, files={"document": ("lecture.pdf", pdf_bytes())}, headers=HEADERS)

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")
    assert_nothing_stored(harness)


def test_two_files_are_400(client: TestClient, harness: DocumentHarness) -> None:
    response = client.post(
        ENDPOINT,
        files=[("file", ("a.pdf", pdf_bytes())), ("file", ("b.pdf", pdf_bytes()))],
        headers=HEADERS,
    )

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")
    assert_nothing_stored(harness)


def test_extra_form_field_is_400(client: TestClient, harness: DocumentHarness) -> None:
    response = client.post(
        ENDPOINT, files={"file": ("lecture.pdf", pdf_bytes())}, data={"topic": "physics"}, headers=HEADERS
    )

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")
    assert_nothing_stored(harness)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("application/json", b'{"file": "x"}'), ("application/pdf", pdf_bytes()), ("multipart/form-data", b"x")],
    ids=["json", "raw pdf", "multipart without a boundary"],
)
def test_body_that_is_not_a_multipart_form_is_400(
    client: TestClient, harness: DocumentHarness, content_type: str, body: bytes
) -> None:
    response = client.post(ENDPOINT, content=body, headers={**HEADERS, "Content-Type": content_type})

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")
    assert_nothing_stored(harness)


@pytest.mark.parametrize("filename", ["", "\u200b", "folder/"])
def test_file_without_a_usable_name_is_400(
    client: TestClient, harness: DocumentHarness, filename: str
) -> None:
    assert_problem(
        upload(client, pdf_bytes(), filename=filename), HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED"
    )

    assert_nothing_stored(harness)


def test_file_at_the_size_limit_is_accepted(client: TestClient) -> None:
    body = assert_document(
        upload(client, padded_pdf(MAX_TEST_DOCUMENT_BYTES)), HTTPStatus.CREATED, UPLOAD_SPEC
    )

    assert body["sizeBytes"] == MAX_TEST_DOCUMENT_BYTES


def test_file_one_byte_over_the_size_limit_is_413_and_never_stored(
    client: TestClient, harness: DocumentHarness
) -> None:
    response = upload(client, padded_pdf(MAX_TEST_DOCUMENT_BYTES + 1))

    assert_problem(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "DOCUMENT_TOO_LARGE")
    assert_nothing_stored(harness)


def test_declared_oversized_body_is_refused_before_the_rate_limit_or_the_body_is_touched(
    client: TestClient, harness: DocumentHarness
) -> None:
    oversized = MAX_TEST_DOCUMENT_BYTES + MULTIPART_OVERHEAD_BYTES + 1

    response = client.post(
        ENDPOINT,
        content=b"x" * oversized,
        headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=x"},
    )

    assert_problem(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "DOCUMENT_TOO_LARGE")
    assert harness.limiter.used == {}
    assert harness.reads == 0


def chunks(content: bytes, size: int) -> Iterator[bytes]:
    for start in range(0, len(content), size):
        yield content[start : start + size]


def test_oversized_body_streamed_without_a_length_is_cut_off_and_never_stored(
    client: TestClient, harness: DocumentHarness
) -> None:
    boundary = "deckly-test-boundary"
    file_part = padded_pdf(MAX_TEST_DOCUMENT_BYTES + MULTIPART_OVERHEAD_BYTES)
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.pdf"\r\n\r\n'.encode()
        + file_part
        + f"\r\n--{boundary}--\r\n".encode()
    )

    response = client.post(
        ENDPOINT,
        content=chunks(body, 4096),
        headers={**HEADERS, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert "content-length" not in {name.lower() for name in response.request.headers}
    assert_problem(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "DOCUMENT_TOO_LARGE")
    assert_nothing_stored(harness)


@pytest.mark.parametrize(
    "content",
    [b"", b"plain text pretending to be a pdf", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504],
    ids=["empty", "renamed text", "legacy ppt"],
)
def test_content_that_is_not_a_pdf_or_pptx_is_415_and_never_stored(
    client: TestClient, harness: DocumentHarness, content: bytes
) -> None:
    response = upload(client, content, filename="lecture.pdf", declared_type=PDF_TYPE)

    assert_problem(response, HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "UNSUPPORTED_DOCUMENT_TYPE")
    assert_nothing_stored(harness)


def test_client_over_its_upload_budget_is_429_with_a_retry_hint(
    client: TestClient, harness: DocumentHarness
) -> None:
    harness.limiter.limit = 0

    response = upload(client, pdf_bytes())

    body = assert_problem(response, HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED")
    assert body["retryAfterSeconds"] == UPLOAD_RETRY_AFTER_SECONDS
    assert response.headers[RETRY_AFTER_HEADER] == str(UPLOAD_RETRY_AFTER_SECONDS)
    assert harness.reads == 0


def test_storage_outage_is_503_and_leaves_nothing_behind(
    client: TestClient, harness: DocumentHarness
) -> None:
    harness.storage.put_failure = storage_outage()

    response = upload(client, pdf_bytes())

    body = assert_problem(response, HTTPStatus.SERVICE_UNAVAILABLE, "UPSTREAM_UNAVAILABLE")
    assert isinstance(body["retryAfterSeconds"], int)
    assert_nothing_stored(harness)


def test_rate_limit_store_outage_is_503_not_a_free_pass(client: TestClient, harness: DocumentHarness) -> None:
    harness.limiter.failure = UpstreamUnavailableError(1)

    assert_problem(upload(client, pdf_bytes()), HTTPStatus.SERVICE_UNAVAILABLE, "UPSTREAM_UNAVAILABLE")
    assert_nothing_stored(harness)


BOUNDARY = "deckly-test-boundary"
type AsgiMessage = MutableMapping[str, object]


PADDING_HEADER_BYTES = 4000


def padding_headers(size: int) -> bytes:
    lines = []
    while size > 0:
        value = b"p" * max(1, min(PADDING_HEADER_BYTES, size - len(b"X-Padding: \r\n")))
        lines.append(b"X-Padding: " + value + b"\r\n")
        size -= len(lines[-1])
    return b"".join(lines)


def multipart_body(file_part: bytes, padding: bytes = b"") -> bytes:
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'.encode()
        + padding
        + b"\r\n"
        + file_part
        + f"\r\n--{BOUNDARY}--\r\n".encode()
    )


def body_of_exactly(size: int) -> bytes:
    file_part = padded_pdf(MAX_TEST_DOCUMENT_BYTES)
    body = multipart_body(file_part, padding_headers(size - len(multipart_body(file_part))))
    assert len(body) == size
    return body


MULTIPART_HEADERS = {**HEADERS, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


@pytest.mark.parametrize("transfer", ["with a length", "chunked"])
def test_body_exactly_at_the_byte_cap_is_read_and_stored(client: TestClient, transfer: str) -> None:
    body = body_of_exactly(body_limit(MAX_TEST_DOCUMENT_BYTES))

    response = client.post(
        ENDPOINT, content=chunks(body, 4096) if transfer == "chunked" else body, headers=MULTIPART_HEADERS
    )

    assert_document(response, HTTPStatus.CREATED, UPLOAD_SPEC)


@pytest.mark.parametrize("transfer", ["with a length", "chunked"])
def test_body_one_byte_past_the_byte_cap_is_413(
    client: TestClient, harness: DocumentHarness, transfer: str
) -> None:
    body = body_of_exactly(body_limit(MAX_TEST_DOCUMENT_BYTES) + 1)

    response = client.post(
        ENDPOINT, content=chunks(body, 4096) if transfer == "chunked" else body, headers=MULTIPART_HEADERS
    )

    assert_problem(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "DOCUMENT_TOO_LARGE")
    assert_nothing_stored(harness)


async def call_asgi(
    harness: DocumentHarness, headers: list[tuple[bytes, bytes]], messages: list[AsgiMessage]
) -> list[AsgiMessage]:
    sent: list[AsgiMessage] = []

    async def receive() -> AsgiMessage:
        return messages.pop(0)

    async def send(message: AsgiMessage) -> None:
        sent.append(message)

    path = f"{ENDPOINT}"
    scope: AsgiMessage = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"x-client-id", str(CLIENT_ID).encode()), *headers],
        "client": ("203.0.113.7", 50000),
        "server": ("testserver", 80),
    }
    await documents_app(harness)(scope, receive, send)
    return sent


def response_status(sent: list[AsgiMessage]) -> object:
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


@pytest.mark.anyio
async def test_client_that_disconnects_mid_upload_is_a_quiet_400_not_an_unhandled_error(
    harness: DocumentHarness, caplog: pytest.LogCaptureFixture
) -> None:
    partial = multipart_body(pdf_bytes())[:120]
    messages: list[AsgiMessage] = [
        {"type": "http.request", "body": partial, "more_body": True},
        {"type": "http.disconnect"},
    ]

    with caplog.at_level(logging.ERROR):
        sent = await call_asgi(
            harness, [(b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())], messages
        )

    assert response_status(sent) == HTTPStatus.BAD_REQUEST
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
    assert_nothing_stored(harness)


@pytest.mark.anyio
@pytest.mark.parametrize("declared", ["\u00b2", "\u00b9\u00b2", "-1", "1e9", ""])
async def test_content_length_that_is_not_plain_ascii_digits_never_breaks_the_size_precheck(
    harness: DocumentHarness, declared: str
) -> None:
    body = multipart_body(pdf_bytes())
    messages: list[AsgiMessage] = [{"type": "http.request", "body": body, "more_body": False}]

    sent = await call_asgi(
        harness,
        [
            (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
            (b"content-length", declared.encode("latin-1")),
        ],
        messages,
    )

    assert response_status(sent) == HTTPStatus.CREATED
