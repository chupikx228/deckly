from dataclasses import dataclass, field
from typing import Protocol

from deckly.infrastructure.resilience import PersistentError, TransientError


@dataclass(frozen=True, slots=True)
class StorageEndpoint:
    endpoint_url: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    timeout_seconds: float


class StorageClient(Protocol):
    async def put_object(self, key: str, content: bytes, content_type: str) -> None: ...

    async def delete_object(self, key: str) -> None: ...

    async def aclose(self) -> None: ...


class StorageError(Exception):
    pass


class StorageUnavailableError(StorageError, TransientError):
    pass


class StorageRejectedError(StorageError, PersistentError):
    pass
