import hashlib
from collections.abc import Callable
from http import HTTPStatus

import httpx2
import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.exceptions import DocumentNotFoundError
from deckly.infrastructure.resilience import CircuitOpenError, ResilientCaller, RetryPolicy
from deckly.infrastructure.storage.client import (
    StorageObjectMissingError,
    StorageRejectedError,
    StorageUnavailableError,
)
from deckly.infrastructure.storage.s3_client import S3StorageClient
from deckly.infrastructure.storage.storage import ResilientDocumentStorage
from tests.documents import FAKE_STORAGE_CREDENTIAL, STORAGE_ENDPOINT, pdf_bytes
from tests.domain.builders import T0
from tests.fakes import ManualTime, fresh_probe

pytestmark = pytest.mark.anyio

KEY = "documents/0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"
OBJECT_PATH = f"/deckly-documents/{KEY}"
POLICY = RetryPolicy(
    max_attempts=2, attempt_timeout_seconds=5, deadline_seconds=20, base_delay_seconds=1, max_delay_seconds=2
)

type Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    def __init__(self, *statuses: int, headers: dict[str, str] | None = None) -> None:
        self.statuses = list(statuses)
        self.headers = headers or {}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx2.Response(status, headers=self.headers, text="<Error><Code>Secret detail</Code></Error>")


def client(handler: Handler) -> S3StorageClient:
    return S3StorageClient(STORAGE_ENDPOINT, httpx2.MockTransport(handler), clock=lambda: T0)


def unreachable(request: httpx2.Request) -> httpx2.Response:
    message = "connection refused"
    raise httpx2.ConnectError(message, request=request)


async def test_put_sends_the_content_signed_to_the_bucket_path() -> None:
    recorder = Recorder(HTTPStatus.OK)
    content = pdf_bytes()

    async with client(recorder) as storage:
        await storage.put_object(KEY, content, "application/pdf")

    [request] = recorder.requests
    assert request.method == "PUT"
    assert request.url.path == OBJECT_PATH
    assert request.content == content
    assert request.headers["content-type"] == "application/pdf"
    assert request.headers["host"] == "storage.example.test"
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(content).hexdigest()
    assert request.headers["x-amz-date"] == "20260814T103000Z"
    assert request.headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=fake-access-key-id/20260814/auto/s3/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert FAKE_STORAGE_CREDENTIAL not in str(request.headers)


@pytest.mark.parametrize("status", [HTTPStatus.NO_CONTENT, HTTPStatus.OK, HTTPStatus.NOT_FOUND])
async def test_delete_of_a_present_or_missing_object_succeeds(status: int) -> None:
    recorder = Recorder(status)

    async with client(recorder) as storage:
        await storage.delete_object(KEY)

    [request] = recorder.requests
    assert request.method == "DELETE"
    assert request.url.path == OBJECT_PATH
    assert request.content == b""


@pytest.mark.parametrize(
    "status",
    [
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.CONFLICT,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.SERVICE_UNAVAILABLE,
    ],
)
async def test_transient_status_is_an_outage_that_keeps_the_retry_hint(status: int) -> None:
    async with client(Recorder(status, headers={"retry-after": "7"})) as storage:
        with pytest.raises(StorageUnavailableError) as raised:
            await storage.put_object(KEY, b"x", "application/pdf")

    assert raised.value.retry_after_seconds == 7


@pytest.mark.parametrize(
    "status", [HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND]
)
async def test_rejected_upload_is_persistent_and_never_echoes_the_provider_body(status: int) -> None:
    async with client(Recorder(status)) as storage:
        with pytest.raises(StorageRejectedError) as raised:
            await storage.put_object(KEY, b"x", "application/pdf")

    assert str(raised.value) == f"Object storage answered {status}"


async def test_rejected_delete_other_than_not_found_is_persistent() -> None:
    async with client(Recorder(HTTPStatus.FORBIDDEN)) as storage:
        with pytest.raises(StorageRejectedError):
            await storage.delete_object(KEY)


async def test_unreachable_storage_is_an_outage() -> None:
    async with client(unreachable) as storage:
        with pytest.raises(StorageUnavailableError):
            await storage.put_object(KEY, b"x", "application/pdf")


def resilient(handler: Handler, time: ManualTime, failure_threshold: int = 5) -> ResilientDocumentStorage:
    def caller() -> ResilientCaller:
        return ResilientCaller(POLICY, time.breaker(failure_threshold), time.runtime(), probe=fresh_probe())

    return ResilientDocumentStorage(
        client=client(handler), upload_caller=caller(), delete_caller=caller(), download_caller=caller()
    )


async def test_transient_failure_is_retried_once_then_succeeds() -> None:
    recorder = Recorder(HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.OK)

    await resilient(recorder, ManualTime()).put(KEY, b"x", "application/pdf")

    assert len(recorder.requests) == 2


async def test_outage_that_outlasts_the_retries_is_upstream_unavailable() -> None:
    recorder = Recorder(HTTPStatus.SERVICE_UNAVAILABLE)

    with pytest.raises(UpstreamUnavailableError):
        await resilient(recorder, ManualTime()).put(KEY, b"x", "application/pdf")

    assert len(recorder.requests) == POLICY.max_attempts


async def test_rejected_request_is_upstream_unavailable_without_a_retry() -> None:
    recorder = Recorder(HTTPStatus.FORBIDDEN)

    with pytest.raises(UpstreamUnavailableError) as raised:
        await resilient(recorder, ManualTime()).delete(KEY)

    assert len(recorder.requests) == 1
    assert isinstance(raised.value.__cause__, StorageRejectedError)


async def test_repeated_rejections_open_the_circuit_so_later_calls_fail_fast() -> None:
    recorder = Recorder(HTTPStatus.FORBIDDEN)
    storage = resilient(recorder, ManualTime(), failure_threshold=2)
    for _ in range(2):
        with pytest.raises(UpstreamUnavailableError):
            await storage.put(KEY, b"x", "application/pdf")

    with pytest.raises(CircuitOpenError):
        await storage.put(KEY, b"x", "application/pdf")

    assert len(recorder.requests) == 2


class Serving:
    def __init__(self, content: bytes, *failures: int) -> None:
        self.content = content
        self.failures = list(failures)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.failures:
            return httpx2.Response(self.failures.pop(0), text="<Error><Code>Secret detail</Code></Error>")
        return httpx2.Response(HTTPStatus.OK, content=self.content)


async def test_get_reads_the_object_with_a_signed_request_to_the_bucket_path() -> None:
    serving = Serving(pdf_bytes())

    async with client(serving) as storage:
        content = await storage.get_object(KEY, max_bytes=len(pdf_bytes()))

    assert content == pdf_bytes()
    [request] = serving.requests
    assert request.method == "GET"
    assert request.url.path == OBJECT_PATH
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()
    assert request.headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=fake-access-key-id/20260814/auto/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="
    )


async def test_get_of_an_object_larger_than_the_cap_is_rejected() -> None:
    async with client(Serving(pdf_bytes())) as storage:
        with pytest.raises(StorageRejectedError, match="more than"):
            await storage.get_object(KEY, max_bytes=len(pdf_bytes()) - 1)


async def test_get_of_a_missing_object_is_missing_not_rejected() -> None:
    async with client(Serving(b"", HTTPStatus.NOT_FOUND)) as storage:
        with pytest.raises(StorageObjectMissingError):
            await storage.get_object(KEY, max_bytes=1024)


async def test_rejected_get_never_echoes_the_provider_body() -> None:
    async with client(Serving(b"", HTTPStatus.FORBIDDEN)) as storage:
        with pytest.raises(StorageRejectedError) as raised:
            await storage.get_object(KEY, max_bytes=1024)

    assert str(raised.value) == f"Object storage answered {HTTPStatus.FORBIDDEN}"


async def test_get_from_unreachable_storage_is_an_outage() -> None:
    async with client(unreachable) as storage:
        with pytest.raises(StorageUnavailableError):
            await storage.get_object(KEY, max_bytes=1024)


async def test_transient_get_failure_is_retried_then_returns_the_object() -> None:
    serving = Serving(pdf_bytes(), HTTPStatus.SERVICE_UNAVAILABLE)

    content = await resilient(serving, ManualTime()).get(KEY, max_bytes=len(pdf_bytes()))

    assert content == pdf_bytes()
    assert len(serving.requests) == 2


async def test_object_gone_from_storage_is_a_missing_document_that_never_opens_the_circuit() -> None:
    serving = Serving(b"", *([HTTPStatus.NOT_FOUND] * 3))
    storage = resilient(serving, ManualTime(), failure_threshold=2)

    for _ in range(3):
        with pytest.raises(DocumentNotFoundError):
            await storage.get(KEY, max_bytes=1024)

    assert len(serving.requests) == 3


async def test_oversized_object_is_upstream_unavailable_without_a_retry() -> None:
    serving = Serving(pdf_bytes())

    with pytest.raises(UpstreamUnavailableError):
        await resilient(serving, ManualTime()).get(KEY, max_bytes=8)

    assert len(serving.requests) == 1
