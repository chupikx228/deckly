import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.infrastructure.resilience import CallSize, ResilientCaller
from deckly.infrastructure.storage.client import StorageClient, StorageError

STORAGE_FAULT_RETRY_AFTER_SECONDS = 5

logger = logging.getLogger(__name__)


@contextmanager
def storage_faults_as_upstream() -> Iterator[None]:
    try:
        yield
    except StorageError as error:
        logger.warning("storage_fault", extra={"error": type(error).__name__})
        raise UpstreamUnavailableError(STORAGE_FAULT_RETRY_AFTER_SECONDS) from error


@dataclass(frozen=True, slots=True)
class ResilientDocumentStorage:
    client: StorageClient
    upload_caller: ResilientCaller
    delete_caller: ResilientCaller

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        with storage_faults_as_upstream():
            await self.upload_caller.call(
                lambda: self.client.put_object(key, content, content_type), size=CallSize.TYPICAL
            )

    async def delete(self, key: str) -> None:
        with storage_faults_as_upstream():
            await self.delete_caller.call(lambda: self.client.delete_object(key), size=CallSize.TYPICAL)
