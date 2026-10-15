import asyncio
import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from http import HTTPStatus
from typing import Self

import httpx2

from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.resilience import RETRY_AFTER_HEADER, is_transient_status, parse_retry_after
from deckly.infrastructure.storage.client import (
    StorageEndpoint,
    StorageError,
    StorageRejectedError,
    StorageUnavailableError,
)
from deckly.infrastructure.storage.signing import (
    EMPTY_PAYLOAD_SHA256,
    CanonicalRequest,
    SigningCredentials,
    encode_path,
    signature_headers,
)

PROVIDER = "Object storage"
HOST_HEADER = "host"
CONTENT_TYPE_HEADER = "content-type"
NO_QUERY: Mapping[str, str] = {}


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def status_error(response: httpx2.Response) -> StorageError:
    message = f"{PROVIDER} answered {response.status_code}"
    if is_transient_status(response.status_code):
        retry_after = parse_retry_after(response.headers.get(RETRY_AFTER_HEADER))
        return StorageUnavailableError(message, retry_after_seconds=retry_after)
    return StorageRejectedError(message)


class S3StorageClient:
    def __init__(
        self,
        endpoint: StorageEndpoint,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._bucket = endpoint.bucket
        self._credentials = SigningCredentials(
            access_key_id=endpoint.access_key_id,
            secret_access_key=endpoint.secret_access_key,
            region=endpoint.region,
        )
        self._clock = clock
        self._client = httpx2.AsyncClient(
            base_url=endpoint.endpoint_url, timeout=endpoint.timeout_seconds, transport=transport
        )
        self._host = self._client.base_url.netloc.decode("ascii")

    async def put_object(self, key: str, content: bytes, content_type: str) -> None:
        payload_sha256 = await asyncio.to_thread(sha256_hex, content)
        request = self._canonical("PUT", key, {CONTENT_TYPE_HEADER: content_type}, payload_sha256)
        response = await self._send(request, content)
        if not response.is_success:
            raise status_error(response)

    async def delete_object(self, key: str) -> None:
        response = await self._send(self._canonical("DELETE", key, {}, EMPTY_PAYLOAD_SHA256), b"")
        if not response.is_success and response.status_code != HTTPStatus.NOT_FOUND:
            raise status_error(response)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _canonical(
        self, method: str, key: str, headers: Mapping[str, str], payload_sha256: str
    ) -> CanonicalRequest:
        return CanonicalRequest(
            method=method,
            path=f"/{self._bucket}/{key}",
            query=NO_QUERY,
            headers={HOST_HEADER: self._host, **headers},
            payload_sha256=payload_sha256,
        )

    async def _send(self, request: CanonicalRequest, content: bytes) -> httpx2.Response:
        signed = signature_headers(request, self._credentials, self._clock())
        try:
            return await self._client.request(
                request.method,
                encode_path(request.path),
                content=content,
                headers={**request.headers, **signed},
            )
        except httpx2.TransportError as error:
            message = f"{PROVIDER} could not be reached: {type(error).__name__}"
            raise StorageUnavailableError(message) from error
        except httpx2.HTTPError as error:
            message = f"{PROVIDER} request failed: {type(error).__name__}"
            raise StorageRejectedError(message) from error
