from datetime import datetime
from http import HTTPStatus

import httpx2

from deckly.config import Settings
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.storage.signing import (
    EMPTY_PAYLOAD_SHA256,
    CanonicalRequest,
    SigningCredentials,
    encode_path,
    signature_headers,
)

BUCKET_READY = frozenset({HTTPStatus.OK, HTTPStatus.CONFLICT})


class ObjectBucket:
    def __init__(self, settings: Settings) -> None:
        providers = settings.providers
        self._bucket = providers.storage_bucket
        self._credentials = SigningCredentials(
            access_key_id=providers.storage_access_key_id,
            secret_access_key=providers.storage_secret_access_key.get_secret_value(),
            region=providers.storage_region,
        )
        self._base_url = str(providers.storage_endpoint_url)

    async def ensure(self) -> None:
        response = await self._send("PUT", f"/{self._bucket}")
        assert response.status_code in BUCKET_READY, response.status_code

    async def holds(self, key: str) -> bool:
        response = await self._send("HEAD", f"/{self._bucket}/{key}")
        assert response.status_code in {HTTPStatus.OK, HTTPStatus.NOT_FOUND}, response.status_code
        return response.status_code == HTTPStatus.OK

    async def read(self, key: str) -> bytes:
        response = await self._send("GET", f"/{self._bucket}/{key}")
        assert response.status_code == HTTPStatus.OK, response.status_code
        return response.content

    async def remove(self, key: str) -> None:
        await self._send("DELETE", f"/{self._bucket}/{key}")

    async def _send(self, method: str, path: str, now: datetime | None = None) -> httpx2.Response:
        async with httpx2.AsyncClient(base_url=self._base_url, timeout=5) as client:
            host = client.base_url.netloc.decode("ascii")
            request = CanonicalRequest(method, path, {}, {"host": host}, EMPTY_PAYLOAD_SHA256)
            signed = signature_headers(request, self._credentials, now or utc_now())
            return await client.request(method, encode_path(path), headers={"host": host, **signed})
