import asyncio
import io
import zipfile
from collections import Counter
from collections.abc import AsyncIterator, Collection, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from deckly.application.documents import GetDocument, UploadDocument
from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.application.ports import DocumentUpload, ExpiredDocument, Requester
from deckly.domain.document import Document, DocumentFormat
from deckly.infrastructure.documents.sniffing import ContentDocumentInspector
from deckly.infrastructure.documents.store import DocumentStatus
from deckly.infrastructure.observability.metrics import DOCUMENT_UPLOAD_GATE
from deckly.infrastructure.storage.client import StorageEndpoint
from deckly.infrastructure.upload_gate import RejectingUploadGate
from tests.domain.builders import T0
from tests.fakes import fresh_metrics

PRESENTATION_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
MACRO_PRESENTATION_MAIN = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
SLIDESHOW_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml"
WORD_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
SHEET_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
UPLOAD_LIMIT = 10
UPLOAD_RETRY_AFTER_SECONDS = 1800
STORAGE_RETRY_AFTER_SECONDS = 5
MAX_TEST_DOCUMENT_BYTES = 64 * 1024
TEST_UPLOAD_CONCURRENCY = 8
TEST_UPLOAD_READ_TIMEOUT_SECONDS = 30.0
ADDRESS = "203.0.113.7"
CLIENT_ID = UUID("0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21")
OTHER_CLIENT_ID = UUID("1c7a8d2f-5b4e-4a3f-8d9c-8b7e6f5a4b32")
FAKE_STORAGE_CREDENTIAL = "fake-storage-credential"
STORAGE_ENDPOINT = StorageEndpoint(
    endpoint_url="https://storage.example.test",
    bucket="deckly-documents",
    region="auto",
    access_key_id="fake-access-key-id",
    secret_access_key=FAKE_STORAGE_CREDENTIAL,
    timeout_seconds=5,
)


def pdf_bytes(
    body: bytes = b"1 0 obj << /Type /Catalog >> endobj\n", *, header: bytes = b"%PDF-1.7\n"
) -> bytes:
    return header + body + b"trailer << /Root 1 0 R >>\n%%EOF\n"


def padded_pdf(size: int) -> bytes:
    return pdf_bytes(b"x" * (size - len(pdf_bytes(b""))))


def content_types(main_type: str, main_part: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/{main_part}" ContentType="{main_type}"/>'
        "</Types>"
    ).encode()


def office_bytes(
    main_type: str = PRESENTATION_MAIN,
    main_part: str = "ppt/presentation.xml",
    extra: dict[str, bytes] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types(main_type, main_part))
        archive.writestr(main_part, b"<presentation/>")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def pptx_bytes() -> bytes:
    return office_bytes()


def sequential_document_ids() -> Iterator[UUID]:
    number = 0
    while True:
        number += 1
        yield UUID(f"00000000-0000-4000-8000-{number:012x}")


def document(number: int = 1, *, client_id: UUID = CLIENT_ID, uploaded_at: datetime = T0) -> Document:
    return Document(
        document_id=UUID(f"00000000-0000-4000-8000-{number:012x}"),
        client_id=client_id,
        filename=f"lecture-{number}.pdf",
        size_bytes=1024,
        document_format=DocumentFormat.PDF,
        uploaded_at=uploaded_at,
    )


@dataclass
class StoredDocument:
    document: Document
    status: DocumentStatus
    used_at: datetime | None = None


class InMemoryDocumentStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_failure: BaseException | None = None
        self.undeletable: dict[str, Exception] = {}
        self.deleted: list[str] = []

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        if self.put_failure is not None:
            raise self.put_failure
        self.objects[key] = (content, content_type)

    async def get(self, key: str, max_bytes: int) -> bytes:
        content, _ = self.objects[key]
        assert len(content) <= max_bytes
        return content

    async def delete(self, key: str) -> None:
        failure = self.undeletable.get(key)
        if failure is not None:
            raise failure
        self.deleted.append(key)
        self.objects.pop(key, None)


@dataclass
class InMemoryExpiredDocuments:
    store: "InMemoryDocumentStore"
    documents: tuple[ExpiredDocument, ...]
    purged: list[UUID] = field(default_factory=list)

    async def purge(self, document_ids: Collection[UUID]) -> int:
        removed = [document_id for document_id in document_ids if document_id in self.store.rows]
        for document_id in removed:
            del self.store.rows[document_id]
        self.purged.extend(removed)
        return len(removed)


class InMemoryDocumentStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, StoredDocument] = {}
        self.failures: dict[str, Exception] = {}
        self.claims: list[InMemoryExpiredDocuments] = []

    async def add_pending(self, document: Document) -> None:
        self._fail_if_broken("add_pending")
        self.rows[document.document_id] = StoredDocument(document=document, status=DocumentStatus.PENDING)

    async def mark_stored(self, document_id: UUID) -> None:
        self._fail_if_broken("mark_stored")
        row = self.rows.get(document_id)
        if row is not None and row.status is DocumentStatus.PENDING:
            self.rows[document_id] = replace(row, status=DocumentStatus.STORED)

    async def discard(self, document_id: UUID) -> None:
        self._fail_if_broken("discard")
        row = self.rows.get(document_id)
        if row is not None and row.status is DocumentStatus.PENDING:
            del self.rows[document_id]

    async def get(self, document_id: UUID, client_id: UUID) -> Document | None:
        self._fail_if_broken("get")
        row = self.rows.get(document_id)
        if row is None or row.status is not DocumentStatus.STORED or row.document.client_id != client_id:
            return None
        return row.document

    def keep(self, document: Document, status: DocumentStatus = DocumentStatus.STORED) -> None:
        self.rows[document.document_id] = StoredDocument(document=document, status=status)

    def mark_used(self, document_id: UUID, at: datetime) -> None:
        self.rows[document_id] = replace(self.rows[document_id], used_at=at)

    @asynccontextmanager
    async def expired(self, uploaded_before: datetime, limit: int) -> AsyncIterator[InMemoryExpiredDocuments]:
        self._fail_if_broken("expired")
        candidates = sorted(
            (
                row.document
                for row in self.rows.values()
                if row.used_at is None and row.document.uploaded_at < uploaded_before
            ),
            key=lambda candidate: candidate.uploaded_at,
        )[:limit]
        claim = InMemoryExpiredDocuments(
            store=self,
            documents=tuple(
                ExpiredDocument(document_id=candidate.document_id, storage_key=candidate.storage_key)
                for candidate in candidates
            ),
        )
        self.claims.append(claim)
        yield claim

    def _fail_if_broken(self, operation: str) -> None:
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure


class InMemoryUploadLimiter:
    def __init__(self, limit: int = UPLOAD_LIMIT) -> None:
        self.limit = limit
        self.used: Counter[UUID] = Counter()
        self.failure: Exception | None = None

    async def acquire(self, requester: Requester, now: datetime) -> None:
        del now
        if self.failure is not None:
            raise self.failure
        self.used[requester.client_id] += 1
        if self.used[requester.client_id] > self.limit:
            raise RateLimitedError(UPLOAD_RETRY_AFTER_SECONDS)


def storage_outage() -> UpstreamUnavailableError:
    return UpstreamUnavailableError(STORAGE_RETRY_AFTER_SECONDS)


class DocumentHarness:
    def __init__(
        self,
        max_bytes: int = MAX_TEST_DOCUMENT_BYTES,
        storage: InMemoryDocumentStorage | None = None,
        concurrency: int = TEST_UPLOAD_CONCURRENCY,
        read_timeout_seconds: float = TEST_UPLOAD_READ_TIMEOUT_SECONDS,
    ) -> None:
        self.metrics = fresh_metrics()
        self.slots = asyncio.Semaphore(concurrency)
        self.limiter = InMemoryUploadLimiter()
        self.storage = storage or InMemoryDocumentStorage()
        self.store = InMemoryDocumentStore()
        self.reads = 0
        ids = sequential_document_ids()
        self.upload = UploadDocument(
            gate=RejectingUploadGate(self.slots, DOCUMENT_UPLOAD_GATE, self.metrics),
            limiter=self.limiter,
            inspector=ContentDocumentInspector(),
            storage=self.storage,
            store=self.store,
            clock=lambda: T0,
            new_document_id=lambda: next(ids),
            max_bytes=max_bytes,
            read_timeout_seconds=read_timeout_seconds,
        )
        self.get = GetDocument(store=self.store)

    def reader(self, content: bytes, filename: str = "lecture.pdf") -> "CountingReader":
        return CountingReader(self, DocumentUpload(filename=filename, content=content))

    async def send(
        self, content: bytes, filename: str = "lecture.pdf", client_id: UUID = CLIENT_ID
    ) -> Document:
        return await self.upload(
            Requester(client_id=client_id, address=ADDRESS), self.reader(content, filename)
        )


@dataclass(frozen=True, slots=True)
class CountingReader:
    harness: DocumentHarness
    upload: DocumentUpload

    async def __call__(self) -> DocumentUpload:
        self.harness.reads += 1
        return self.upload


class LosesPutResponse(InMemoryDocumentStorage):
    async def put(self, key: str, content: bytes, content_type: str) -> None:
        self.objects[key] = (content, content_type)
        raise storage_outage()
