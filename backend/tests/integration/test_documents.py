import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.housekeeping import DocumentRetentionPolicy, EnforceDocumentRetention
from deckly.config import Settings
from deckly.domain.document import Document, DocumentFormat
from deckly.infrastructure.documents.store import (
    DocumentStatus,
    PostgresDocumentHousekeeping,
    PostgresDocumentStore,
)
from deckly.infrastructure.tables import DocumentRow
from deckly.infrastructure.upload_limit import ADDRESS_KEY_PREFIX, CLIENT_KEY_PREFIX
from deckly.main import API_PREFIX, create_app
from deckly.worker.settings import (
    ENFORCE_DOCUMENT_RETENTION_KEY,
    OBSERVABILITY_KEY,
    SETTINGS_KEY,
    WorkerContext,
    build_document_storage,
    build_storage_client,
    from_context,
    shutdown,
    startup,
)
from tests.documents import InMemoryDocumentStorage, pdf_bytes, pptx_bytes
from tests.fakes import fresh_observability
from tests.integration.conftest import TEST_PEER_ADDRESSES, open_queue_pool
from tests.integration.storage import ObjectBucket
from tests.reserved_clients import reserved_client_id
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ENDPOINT = f"{API_PREFIX}/documents"
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)
RETENTION = timedelta(hours=24)
SWEPT_AT = ANCIENT + timedelta(days=3)
CUTOFF = SWEPT_AT - RETENTION
TICK = timedelta(microseconds=1)
BATCH_SIZE = 1000
UNREACHABLE_STORAGE = "http://127.0.0.1:1"


class DocumentCleanup:
    def __init__(
        self, settings: Settings, session_factory: async_sessionmaker[AsyncSession], bucket: ObjectBucket
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.bucket = bucket
        self.client_ids: set[UUID] = set()
        self.keys: set[str] = set()

    def client_id(self) -> UUID:
        client_id = reserved_client_id()
        self.client_ids.add(client_id)
        return client_id

    async def purge(self) -> None:
        async with self.session_factory.begin() as session:
            removed = await session.scalars(
                delete(DocumentRow)
                .where(DocumentRow.client_id.in_(self.client_ids))
                .returning(DocumentRow.storage_key)
            )
            self.keys.update(removed.all())
        for key in self.keys:
            await self.bucket.remove(key)
        pool = await open_queue_pool(self.settings)
        try:
            patterns = [f"{CLIENT_KEY_PREFIX}:{client_id}:*" for client_id in self.client_ids] + [
                f"{ADDRESS_KEY_PREFIX}:{address}:*" for address in TEST_PEER_ADDRESSES
            ]
            for pattern in patterns:
                async for key in pool.scan_iter(match=pattern):
                    await pool.delete(key)
        finally:
            await pool.aclose()


@pytest.fixture
async def bucket(settings: Settings) -> ObjectBucket:
    ready = ObjectBucket(settings)
    await ready.ensure()
    return ready


@pytest.fixture
async def documents(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], bucket: ObjectBucket
) -> AsyncIterator[DocumentCleanup]:
    tracker = DocumentCleanup(settings, session_factory, bucket)
    try:
        yield tracker
    finally:
        await tracker.purge()


@contextmanager
def app_client(settings: Settings) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        with TestClient(create_app(settings)) as test_client:
            yield test_client
    finally:
        root.handlers, root.level = handlers, level


def with_storage(settings: Settings, **overrides: object) -> Settings:
    return replace(settings, providers=settings.providers.model_copy(update=overrides))


async def stored_row(
    session_factory: async_sessionmaker[AsyncSession], document_id: UUID
) -> DocumentRow | None:
    async with session_factory() as session:
        return await session.get(DocumentRow, document_id)


async def rows_of(session_factory: async_sessionmaker[AsyncSession], client_id: UUID) -> list[DocumentRow]:
    async with session_factory() as session:
        return list(await session.scalars(select(DocumentRow).where(DocumentRow.client_id == client_id)))


def upload(
    client: TestClient, client_id: UUID, content: bytes, filename: str = "lecture.pdf"
) -> dict[str, object]:
    response = client.post(
        ENDPOINT,
        files={"file": (filename, content, "application/octet-stream")},
        headers={"X-Client-Id": str(client_id)},
    )
    body: dict[str, object] = response.json()
    body["status"] = response.status_code
    return body


@pytest.mark.parametrize(
    ("content", "content_type"),
    [(pdf_bytes(), DocumentFormat.PDF), (pptx_bytes(), DocumentFormat.PPTX)],
    ids=["pdf", "pptx"],
)
async def test_uploaded_document_has_both_a_stored_record_and_its_object_and_reads_back(
    settings: Settings, documents: DocumentCleanup, content: bytes, content_type: DocumentFormat
) -> None:
    client_id = documents.client_id()
    with app_client(settings) as client:
        body = upload(client, client_id, content)
        read = client.get(f"{ENDPOINT}/{body['documentId']}", headers={"X-Client-Id": str(client_id)})
        foreign = client.get(f"{ENDPOINT}/{body['documentId']}", headers={"X-Client-Id": str(uuid4())})

    assert body.pop("status") == HTTPStatus.CREATED, body
    assert spec_errors("Document", body) == []
    assert body["contentType"] == content_type
    row = await stored_row(documents.session_factory, UUID(str(body["documentId"])))
    assert row is not None
    assert row.status == DocumentStatus.STORED
    assert row.used_at is None
    assert await documents.bucket.read(row.storage_key) == content
    assert read.status_code == HTTPStatus.OK
    assert read.json() == body
    assert foreign.status_code == HTTPStatus.NOT_FOUND
    assert foreign.json()["code"] == "DOCUMENT_NOT_FOUND"


async def test_oversized_upload_is_refused_before_anything_is_stored(
    settings: Settings, documents: DocumentCleanup
) -> None:
    small = replace(settings, limits=settings.limits.model_copy(update={"document_max_bytes": 1024}))
    client_id = documents.client_id()
    with app_client(small) as client:
        body = upload(client, client_id, pdf_bytes(b"x" * 2048))

    assert body["status"] == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert body["code"] == "DOCUMENT_TOO_LARGE"
    assert await rows_of(documents.session_factory, client_id) == []


async def test_renamed_text_file_is_refused_by_its_content(
    settings: Settings, documents: DocumentCleanup
) -> None:
    client_id = documents.client_id()
    with app_client(settings) as client:
        body = upload(client, client_id, b"definitely not a pdf", filename="lecture.pdf")

    assert body["status"] == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert body["code"] == "UNSUPPORTED_DOCUMENT_TYPE"
    assert await rows_of(documents.session_factory, client_id) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"storage_endpoint_url": AnyHttpUrl(UNREACHABLE_STORAGE), "storage_upload_deadline_seconds": 2},
        {"storage_secret_access_key": SecretStr("not-the-right-credential")},
    ],
    ids=["storage down", "storage refuses the credentials"],
)
async def test_storage_failure_is_a_503_that_leaves_nothing_a_client_can_read(
    settings: Settings, documents: DocumentCleanup, overrides: dict[str, object]
) -> None:
    client_id = documents.client_id()
    with app_client(with_storage(settings, **overrides)) as client:
        body = upload(client, client_id, pdf_bytes())

    assert body["status"] == HTTPStatus.SERVICE_UNAVAILABLE, body
    assert body["code"] == "UPSTREAM_UNAVAILABLE"
    leftovers = await rows_of(documents.session_factory, client_id)
    assert all(row.status == DocumentStatus.PENDING for row in leftovers)
    store = PostgresDocumentStore(documents.session_factory)
    assert [await store.get(row.document_id, client_id) for row in leftovers] == [None] * len(leftovers)


async def test_upload_budget_trips_after_the_configured_number_of_uploads(
    settings: Settings, documents: DocumentCleanup
) -> None:
    limited = replace(
        settings,
        limits=settings.limits.model_copy(
            update={"document_uploads_per_window": 2, "document_uploads_per_address_per_window": 50}
        ),
    )
    client_id = documents.client_id()
    with app_client(limited) as client:
        statuses = [upload(client, client_id, pdf_bytes())["status"] for _ in range(3)]
        other = upload(client, documents.client_id(), pdf_bytes())["status"]

    assert statuses == [HTTPStatus.CREATED, HTTPStatus.CREATED, HTTPStatus.TOO_MANY_REQUESTS]
    assert other == HTTPStatus.CREATED
    assert len(await rows_of(documents.session_factory, client_id)) == 2


def ancient_document(client_id: UUID, uploaded_at: datetime) -> Document:
    return Document(
        document_id=uuid4(),
        client_id=client_id,
        filename="lecture.pdf",
        size_bytes=len(pdf_bytes()),
        document_format=DocumentFormat.PDF,
        uploaded_at=uploaded_at,
    )


class StorageProbe:
    def __init__(self, settings: Settings, pause_seconds: float = 0) -> None:
        self.client = build_storage_client(settings.providers)
        self.storage = build_document_storage(settings.providers, self.client, fresh_observability())
        self.pause_seconds = pause_seconds
        self.deleted: list[str] = []

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        await self.storage.put(key, content, content_type)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        await asyncio.sleep(self.pause_seconds)
        await self.storage.delete(key)


async def placed(
    store: PostgresDocumentStore, storage: StorageProbe, document: Document, *, stored: bool = True
) -> Document:
    await store.add_pending(document)
    if stored:
        await storage.put(document.storage_key, pdf_bytes(), document.document_format)
        await store.mark_stored(document.document_id)
    return document


def sweeper(
    session_factory: async_sessionmaker[AsyncSession], storage: StorageProbe | InMemoryDocumentStorage
) -> EnforceDocumentRetention:
    return EnforceDocumentRetention(
        housekeeping=PostgresDocumentHousekeeping(session_factory),
        storage=storage,
        policy=DocumentRetentionPolicy(retention=RETENTION, batch_size=BATCH_SIZE, max_concurrency=4),
        clock=lambda: SWEPT_AT,
    )


@pytest.fixture
async def probe(settings: Settings) -> AsyncIterator[StorageProbe]:
    storage = StorageProbe(settings)
    try:
        yield storage
    finally:
        await storage.client.aclose()


async def test_sweep_deletes_old_unused_documents_and_keeps_recent_and_used_ones(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory, bucket = documents.session_factory, documents.bucket
    store = PostgresDocumentStore(session_factory)
    client_id = documents.client_id()
    expired = await placed(store, probe, ancient_document(client_id, CUTOFF - TICK))
    abandoned = await placed(store, probe, ancient_document(client_id, CUTOFF - TICK), stored=False)
    recent = await placed(store, probe, ancient_document(client_id, CUTOFF))
    used = await placed(store, probe, ancient_document(client_id, ANCIENT))
    async with session_factory.begin() as session:
        await session.execute(
            update(DocumentRow).where(DocumentRow.document_id == used.document_id).values(used_at=ANCIENT)
        )

    purged = await sweeper(session_factory, probe)()

    assert purged >= 2
    remaining = {row.document_id for row in await rows_of(session_factory, client_id)}
    assert remaining == {recent.document_id, used.document_id}
    assert not await bucket.holds(expired.storage_key)
    assert not await bucket.holds(abandoned.storage_key)
    assert await bucket.holds(recent.storage_key)
    assert await bucket.holds(used.storage_key)


async def test_record_survives_when_its_object_cannot_be_deleted(
    settings: Settings, documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    store = PostgresDocumentStore(session_factory)
    client_id = documents.client_id()
    stuck = await placed(store, probe, ancient_document(client_id, CUTOFF - TICK))
    down = StorageProbe(with_storage(settings, storage_endpoint_url=AnyHttpUrl(UNREACHABLE_STORAGE)))
    try:
        await sweeper(session_factory, down)()
    finally:
        await down.client.aclose()

    assert [row.document_id for row in await rows_of(session_factory, client_id)] == [stuck.document_id]


async def test_concurrent_sweeps_never_delete_the_same_document_twice(
    settings: Settings, documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    store = PostgresDocumentStore(session_factory)
    client_id = documents.client_id()
    placed_documents = [
        await placed(store, probe, ancient_document(client_id, CUTOFF - TICK)) for _ in range(6)
    ]
    first, second = StorageProbe(settings, pause_seconds=0.2), StorageProbe(settings, pause_seconds=0.2)
    try:
        await asyncio.gather(sweeper(session_factory, first)(), sweeper(session_factory, second)())
    finally:
        await first.client.aclose()
        await second.client.aclose()

    ours = {document.storage_key for document in placed_documents}
    swept = [key for key in first.deleted + second.deleted if key in ours]
    assert sorted(swept) == sorted(ours)
    assert await rows_of(session_factory, client_id) == []


async def test_pending_and_foreign_documents_are_invisible_and_only_pending_ones_can_be_discarded(
    documents: DocumentCleanup,
) -> None:
    session_factory = documents.session_factory
    store = PostgresDocumentStore(session_factory)
    owner = documents.client_id()
    pending = ancient_document(owner, ANCIENT)
    stored = ancient_document(owner, ANCIENT)
    await store.add_pending(pending)
    await store.add_pending(stored)
    await store.mark_stored(stored.document_id)

    await store.discard(stored.document_id)
    await store.discard(pending.document_id)

    assert await store.get(pending.document_id, owner) is None
    assert await store.get(stored.document_id, owner) == stored
    assert await store.get(stored.document_id, documents.client_id()) is None
    assert await stored_row(session_factory, pending.document_id) is None


async def test_worker_startup_wires_document_retention_with_the_configured_policy(settings: Settings) -> None:
    ctx: WorkerContext = {SETTINGS_KEY: settings, OBSERVABILITY_KEY: fresh_observability()}

    await startup(ctx)
    try:
        retention = from_context(ctx, ENFORCE_DOCUMENT_RETENTION_KEY, EnforceDocumentRetention)
    finally:
        await shutdown(ctx)

    assert retention.policy.retention == timedelta(seconds=settings.cache.document_retention_seconds)
    assert retention.policy.batch_size == settings.sweep.document_batch_size
    assert retention.policy.max_concurrency == settings.providers.storage_max_concurrency
