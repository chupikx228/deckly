import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.generations import Clock
from deckly.application.ports import (
    ArticleFetcher,
    DocumentInspector,
    DocumentStorage,
    DocumentStore,
    DocumentUpload,
    RecordingFetcher,
    Requester,
    UploadGate,
    UploadLimiter,
    UploadReader,
)
from deckly.domain.article_url import parse_article_url
from deckly.domain.document import Document, clean_filename
from deckly.domain.exceptions import DocumentNotFoundError, DocumentTooLargeError
from deckly.domain.recording_url import parse_youtube_video

logger = logging.getLogger(__name__)

type DocumentIdFactory = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class UploadDocument:
    gate: UploadGate
    limiter: UploadLimiter
    inspector: DocumentInspector
    storage: DocumentStorage
    store: DocumentStore
    clock: Clock
    new_document_id: DocumentIdFactory
    max_bytes: int
    read_timeout_seconds: float

    async def __call__(self, requester: Requester, read: UploadReader) -> Document:
        async with self.gate.admit():
            return await self._upload(requester, read)

    async def _upload(self, requester: Requester, read: UploadReader) -> Document:
        await self.limiter.acquire(requester, self.clock())
        upload = await read()
        document = await self._admit(upload, requester.client_id)
        await self.store.add_pending(document)
        try:
            await self.storage.put(document.storage_key, upload.content, document.document_format)
        except BaseException:
            await self._withdraw(document)
            raise
        await self.store.mark_stored(document.document_id)
        logger.info(
            "document_uploaded",
            extra={
                "document_id": str(document.document_id),
                "size_bytes": document.size_bytes,
                "content_type": document.document_format,
                "duration_seconds": document.duration_seconds,
            },
        )
        return document

    async def _admit(self, upload: DocumentUpload, client_id: UUID) -> Document:
        size_bytes = len(upload.content)
        if size_bytes > self.max_bytes:
            message = f"the document is {size_bytes} bytes, over the {self.max_bytes} byte limit"
            raise DocumentTooLargeError(message)
        filename = clean_filename(upload.filename)
        inspected = await self.inspector.identify(upload.content)
        return Document(
            document_id=self.new_document_id(),
            client_id=client_id,
            filename=filename,
            size_bytes=size_bytes,
            document_format=inspected.document_format,
            uploaded_at=self.clock(),
            duration_seconds=inspected.duration_seconds,
            source_url=upload.source_url,
        )

    async def _withdraw(self, document: Document) -> None:
        context = {"document_id": str(document.document_id)}
        try:
            await self.storage.delete(document.storage_key)
        except UpstreamUnavailableError as error:
            logger.warning("document_left_for_sweep", extra={**context, "error": type(error).__name__})
            return
        except Exception:
            logger.exception("document_left_for_sweep", extra=context)
            return
        try:
            await self.store.discard(document.document_id)
        except Exception:
            logger.exception("document_not_discarded", extra=context)


@dataclass(frozen=True, slots=True)
class FetchRecording:
    upload: UploadDocument
    fetcher: RecordingFetcher

    async def __call__(self, requester: Requester, url: str) -> Document:
        video = parse_youtube_video(url)
        return await self.upload(requester, partial(self.fetcher.fetch, video))


@dataclass(frozen=True, slots=True)
class FetchArticle:
    upload: UploadDocument
    fetcher: ArticleFetcher

    async def __call__(self, requester: Requester, url: str) -> Document:
        article = parse_article_url(url)
        return await self.upload(requester, partial(self.fetcher.fetch, article))


@dataclass(frozen=True, slots=True)
class GetDocument:
    store: DocumentStore

    async def __call__(self, document_id: UUID, client_id: UUID) -> Document:
        document = await self.store.get(document_id, client_id)
        if document is None:
            message = f"document {document_id} does not exist for this client"
            raise DocumentNotFoundError(message)
        return document
