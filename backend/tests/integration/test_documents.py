import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.documents import UploadDocument
from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.housekeeping import DocumentRetentionPolicy, EnforceDocumentRetention
from deckly.application.pipeline import RunGeneration
from deckly.application.ports import IdempotencyScope
from deckly.config import Settings
from deckly.domain.document import Document, DocumentFormat, storage_key
from deckly.domain.exceptions import DocumentNotFoundError
from deckly.domain.job import GenerationJob
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import CitationUnit, DocumentSource
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.documents.parser import DocumentTextParser
from deckly.infrastructure.documents.store import (
    DocumentStatus,
    PostgresDocumentHousekeeping,
    PostgresDocumentStore,
)
from deckly.infrastructure.job_store import PostgresJobHousekeeping, PostgresJobStore
from deckly.infrastructure.moderation.moderator import LlmContentModerator, LlmMaterialModerator
from deckly.infrastructure.observability.metrics import DOCUMENT_UPLOAD_GATE, RECORDING_UPLOAD_GATE
from deckly.infrastructure.recordings.sniffing import detect_recording
from deckly.infrastructure.tables import DocumentRow, GenerationJobRow
from deckly.infrastructure.upload_gate import UPLOAD_CAPACITY_RETRY_AFTER_SECONDS, RejectingUploadGate
from deckly.infrastructure.upload_limit import ADDRESS_KEY_PREFIX, CLIENT_KEY_PREFIX
from deckly.main import API_PREFIX, create_app
from deckly.worker.settings import (
    ENFORCE_DOCUMENT_RETENTION_KEY,
    OBSERVABILITY_KEY,
    RUN_GENERATION_KEY,
    SETTINGS_KEY,
    WorkerContext,
    build_document_storage,
    build_storage_client,
    from_context,
    shutdown,
    startup,
)
from tests.document_files import TOPIC_TEXT, text_pdf
from tests.documents import InMemoryDocumentStorage, pdf_bytes, pptx_bytes
from tests.fakes import (
    FakeLlmClient,
    InMemoryResultCache,
    document_request,
    fresh_observability,
    generation_request,
    model_reply,
)
from tests.ffmpeg import extract_audio
from tests.integration.conftest import TEST_PEER_ADDRESSES, Cleanup, open_queue_pool
from tests.integration.storage import ObjectBucket
from tests.recording_files import fixture, pcm_wave
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
            owned = select(DocumentRow.document_id).where(DocumentRow.client_id.in_(self.client_ids))
            await session.execute(delete(GenerationJobRow).where(GenerationJobRow.document_id.in_(owned)))
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


def with_upload_concurrency(settings: Settings, concurrency: int) -> Settings:
    return replace(
        settings, limits=settings.limits.model_copy(update={"document_upload_max_concurrency": concurrency})
    )


def gate_samples(scraped: str) -> dict[str, float]:
    marker = f'gate="{DOCUMENT_UPLOAD_GATE}"'
    return {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in scraped.splitlines()
        if line.startswith("deckly_gate_") and marker in line
    }


async def test_upload_gate_reports_its_limit_and_its_uploads_on_the_metrics_endpoint(
    settings: Settings, documents: DocumentCleanup
) -> None:
    client_id = documents.client_id()
    with app_client(with_upload_concurrency(settings, 3)) as client:
        before = gate_samples(client.get("/metrics").text)
        body = upload(client, client_id, pdf_bytes())
        after = gate_samples(client.get("/metrics").text)

    assert body["status"] == HTTPStatus.CREATED
    assert before == {f'deckly_gate_limit{{gate="{DOCUMENT_UPLOAD_GATE}"}}': 3.0}
    assert after[f'deckly_gate_limit{{gate="{DOCUMENT_UPLOAD_GATE}"}}'] == 3.0
    assert after[f'deckly_gate_in_flight{{gate="{DOCUMENT_UPLOAD_GATE}"}}'] == 0.0
    assert after[f'deckly_gate_wait_seconds_count{{gate="{DOCUMENT_UPLOAD_GATE}",outcome="acquired"}}'] == 1.0
    assert after[f'deckly_gate_wait_seconds_sum{{gate="{DOCUMENT_UPLOAD_GATE}",outcome="acquired"}}'] == 0.0


async def test_upload_arriving_while_every_slot_is_taken_is_503_and_does_not_spend_the_clients_budget(
    settings: Settings, documents: DocumentCleanup
) -> None:
    client_id = documents.client_id()
    with app_client(with_upload_concurrency(settings, 1)) as client:
        assert isinstance(client.app, FastAPI)
        use_case = client.app.state.upload_document
        assert isinstance(use_case, UploadDocument)
        assert isinstance(use_case.gate, RejectingUploadGate)
        await use_case.gate.gate.acquire()
        refused = client.post(
            ENDPOINT,
            files={"file": ("lecture.pdf", pdf_bytes(), "application/octet-stream")},
            headers={"X-Client-Id": str(client_id)},
        )
        use_case.gate.gate.release()
        accepted = upload(client, client_id, pdf_bytes())
        scraped = client.get("/metrics").text

    assert refused.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert refused.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert refused.headers["Retry-After"] == str(UPLOAD_CAPACITY_RETRY_AFTER_SECONDS)
    assert accepted["status"] == HTTPStatus.CREATED
    assert len(await rows_of(documents.session_factory, client_id)) == 1
    assert f'deckly_gate_rejections_total{{gate="{DOCUMENT_UPLOAD_GATE}"}} 1.0' in scraped
    pool = await open_queue_pool(settings)
    try:
        keys = [key async for key in pool.scan_iter(match=f"{CLIENT_KEY_PREFIX}:{client_id}:*")]
        counted = [await pool.get(key) for key in keys]
    finally:
        await pool.aclose()
    assert [int(count) for count in counted if count is not None] == [1]


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
    store = document_store(documents.session_factory)
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


def document_store(session_factory: async_sessionmaker[AsyncSession]) -> PostgresDocumentStore:
    return PostgresDocumentStore(session_factory, retention=RETENTION, clock=lambda: SWEPT_AT)


async def generate_from(
    session_factory: async_sessionmaker[AsyncSession], document: Document
) -> GenerationJob:
    job = GenerationJob.queue(uuid4(), document.uploaded_at)
    scope = IdempotencyScope(client_id=document.client_id, idempotency_key=uuid4())
    store = PostgresJobStore(session_factory, document_retention=RETENTION)
    await store.add(job, document_request(document.document_id), scope)
    return job


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

    async def get(self, key: str, max_bytes: int) -> bytes:
        return await self.storage.get(key, max_bytes)

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


async def test_stored_document_reads_back_byte_for_byte_and_parses_into_cited_material(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    client_id = documents.client_id()
    lecture = replace(ancient_document(client_id, SWEPT_AT), size_bytes=len(text_pdf(TOPIC_TEXT)))
    documents.keys.add(lecture.storage_key)
    await probe.put(lecture.storage_key, text_pdf(TOPIC_TEXT), lecture.document_format)

    content = await probe.get(lecture.storage_key, lecture.size_bytes)
    material = await DocumentTextParser(max_characters=4000, deadline_seconds=30).parse(lecture, content)

    assert content == text_pdf(TOPIC_TEXT)
    assert [entry.source for entry in material] == [
        DocumentSource(
            title=lecture.filename, document_id=lecture.document_id, unit=CitationUnit.PAGE, number=1
        )
    ]


async def test_reading_an_object_that_is_not_stored_is_a_missing_document(probe: StorageProbe) -> None:
    with pytest.raises(DocumentNotFoundError):
        await probe.get(storage_key(uuid4()), 1024)


async def test_object_larger_than_its_recorded_size_is_refused(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    key = storage_key(uuid4())
    documents.keys.add(key)
    await probe.put(key, pdf_bytes(), DocumentFormat.PDF)

    with pytest.raises(UpstreamUnavailableError):
        await probe.get(key, len(pdf_bytes()) - 1)


async def test_sweep_deletes_old_unused_documents_and_keeps_recent_and_used_ones(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory, bucket = documents.session_factory, documents.bucket
    store = document_store(session_factory)
    client_id = documents.client_id()
    expired = await placed(store, probe, ancient_document(client_id, CUTOFF - TICK))
    abandoned = await placed(store, probe, ancient_document(client_id, CUTOFF - TICK), stored=False)
    recent = await placed(store, probe, ancient_document(client_id, CUTOFF))
    used = await placed(store, probe, ancient_document(client_id, ANCIENT))
    await generate_from(session_factory, used)

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
    store = document_store(session_factory)
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
    store = document_store(session_factory)
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
    store = document_store(session_factory)
    owner = documents.client_id()
    pending = ancient_document(owner, SWEPT_AT)
    stored = ancient_document(owner, SWEPT_AT)
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


JOB_TIME = SWEPT_AT
GENERATIONS_ENDPOINT = f"{API_PREFIX}/generations"
CARD_REPLY = {
    "deck": {"title": "Road signs", "description": "Warning signs", "tags": ["signs"]},
    "notes": [
        {
            "noteType": NoteType.BASIC,
            "fields": {"front": "What does a red triangle warn of?", "back": "Danger ahead"},
            "sources": [1],
        }
    ],
}
ALLOW = {"verdict": "allow"}
EVERY_NOTE_ALLOWED = {"deck": "allow", "notes": {"1": "allow"}}


def job_store(session_factory: async_sessionmaker[AsyncSession]) -> PostgresJobStore:
    return PostgresJobStore(session_factory, document_retention=RETENTION)


def scope_of(client_id: UUID) -> IdempotencyScope:
    return IdempotencyScope(client_id=client_id, idempotency_key=uuid4())


async def jobs_referencing(
    session_factory: async_sessionmaker[AsyncSession], document_id: UUID
) -> list[GenerationJobRow]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(GenerationJobRow).where(GenerationJobRow.document_id == document_id)
        )
        return list(rows)


async def test_generation_claims_its_document_in_the_transaction_that_stores_the_job(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    lecture = await placed(
        document_store(session_factory), probe, ancient_document(documents.client_id(), CUTOFF)
    )
    store = job_store(session_factory)
    first = GenerationJob.queue(uuid4(), JOB_TIME)
    later = GenerationJob.queue(uuid4(), JOB_TIME + timedelta(minutes=5))
    scope = scope_of(lecture.client_id)

    stored = await store.add(first, document_request(lecture.document_id), scope)
    await store.add(later, document_request(lecture.document_id), scope_of(lecture.client_id))

    row = await stored_row(session_factory, lecture.document_id)
    assert row is not None
    assert row.used_at == first.created_at
    assert {job.job_id for job in await jobs_referencing(session_factory, lecture.document_id)} == {
        first.job_id,
        later.job_id,
    }
    assert stored.request.topic is None
    assert await store.get_stored(first.job_id) == stored
    assert await store.find(scope) == stored


@pytest.mark.parametrize(
    "case", ["pending", "another client's", "never uploaded", "expired and unused"], ids=str
)
async def test_document_the_client_cannot_use_is_refused_and_leaves_no_job_or_claim_behind(
    documents: DocumentCleanup, probe: StorageProbe, case: str
) -> None:
    session_factory = documents.session_factory
    owner = documents.client_id()
    uploaded_at = CUTOFF - TICK if case == "expired and unused" else CUTOFF
    lecture = ancient_document(owner, uploaded_at)
    if case != "never uploaded":
        await placed(document_store(session_factory), probe, lecture, stored=case != "pending")
    client_id = documents.client_id() if case == "another client's" else owner
    scope = scope_of(client_id)

    with pytest.raises(DocumentNotFoundError):
        await job_store(session_factory).add(
            GenerationJob.queue(uuid4(), JOB_TIME), document_request(lecture.document_id), scope
        )

    assert await job_store(session_factory).find(scope) is None
    row = await stored_row(session_factory, lecture.document_id)
    assert row is None or row.used_at is None


async def test_expired_document_a_job_still_refers_to_can_be_used_again(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    lecture = await placed(
        document_store(session_factory), probe, ancient_document(documents.client_id(), ANCIENT)
    )
    await generate_from(session_factory, lecture)
    again = GenerationJob.queue(uuid4(), JOB_TIME)

    stored = await job_store(session_factory).add(
        again, document_request(lecture.document_id), scope_of(lecture.client_id)
    )

    assert stored.job == again
    row = await stored_row(session_factory, lecture.document_id)
    assert row is not None
    assert row.used_at == ANCIENT


async def test_claim_is_rolled_back_when_the_idempotency_key_already_names_another_job(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    lecture = await placed(
        document_store(session_factory), probe, ancient_document(documents.client_id(), CUTOFF)
    )
    store = job_store(session_factory)
    scope = scope_of(lecture.client_id)
    original = await store.add(GenerationJob.queue(uuid4(), JOB_TIME), generation_request(), scope)

    replay = await store.add(
        GenerationJob.queue(uuid4(), JOB_TIME), document_request(lecture.document_id), scope
    )

    assert replay == original
    row = await stored_row(session_factory, lecture.document_id)
    assert row is not None
    assert row.used_at is None
    assert await jobs_referencing(session_factory, lecture.document_id) == []


async def test_database_refuses_to_delete_a_document_a_job_refers_to(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    lecture = await placed(
        document_store(session_factory), probe, ancient_document(documents.client_id(), CUTOFF)
    )
    await generate_from(session_factory, replace(lecture, uploaded_at=JOB_TIME))

    with pytest.raises(IntegrityError):
        async with session_factory.begin() as session:
            await session.execute(delete(DocumentRow).where(DocumentRow.document_id == lecture.document_id))

    assert await stored_row(session_factory, lecture.document_id) is not None


async def test_used_document_survives_the_sweep_until_job_retention_purges_its_last_job(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    lecture = await placed(
        document_store(session_factory), probe, ancient_document(documents.client_id(), ANCIENT)
    )
    job = await generate_from(session_factory, lecture)
    finished_at = ANCIENT + timedelta(minutes=1)
    await job_store(session_factory).update(job.job_id, lambda stored: stored.cancel(finished_at))

    await sweeper(session_factory, probe)()
    kept = await stored_row(session_factory, lecture.document_id)
    purged_jobs = await PostgresJobHousekeeping(session_factory).purge_finished(SWEPT_AT, BATCH_SIZE)
    await sweeper(session_factory, probe)()

    assert kept is not None
    assert purged_jobs >= 1
    assert await stored_row(session_factory, lecture.document_id) is None
    assert not await documents.bucket.holds(lecture.storage_key)


class HeldDelete(InMemoryDocumentStorage):
    def __init__(self) -> None:
        super().__init__()
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    async def delete(self, key: str) -> None:
        self.holding.set()
        await self.release.wait()
        await super().delete(key)


async def test_generation_whose_clock_still_sees_the_document_waits_for_the_sweep_and_is_refused(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    lecture = await placed(
        document_store(session_factory), probe, ancient_document(documents.client_id(), CUTOFF - TICK)
    )
    held = HeldDelete()
    sweep = asyncio.create_task(sweeper(session_factory, held)())
    await held.holding.wait()
    scope = scope_of(lecture.client_id)

    admission = asyncio.create_task(
        job_store(session_factory).add(
            GenerationJob.queue(uuid4(), JOB_TIME - timedelta(hours=1)),
            document_request(lecture.document_id),
            scope,
        )
    )
    await asyncio.sleep(0.2)
    blocked = not admission.done()
    held.release.set()
    await sweep

    assert blocked
    with pytest.raises(DocumentNotFoundError):
        await admission
    assert await stored_row(session_factory, lecture.document_id) is None
    assert await job_store(session_factory).find(scope) is None


async def test_reading_a_document_follows_the_same_retention_rule_as_using_it(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory = documents.session_factory
    store = document_store(session_factory)
    owner = documents.client_id()
    boundary = await placed(store, probe, ancient_document(owner, CUTOFF))
    expired = await placed(store, probe, ancient_document(owner, CUTOFF - TICK))
    referenced = await placed(store, probe, ancient_document(owner, ANCIENT))
    await generate_from(session_factory, referenced)

    assert await store.get(boundary.document_id, owner) == boundary
    assert await store.get(expired.document_id, owner) is None
    assert await store.get(referenced.document_id, owner) == referenced


def generation_headers(client_id: UUID) -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4()), "X-Client-Id": str(client_id)}


def start_generation(client: TestClient, client_id: UUID, body: dict[str, object]) -> dict[str, object]:
    response = client.post(GENERATIONS_ENDPOINT, json=body, headers=generation_headers(client_id))
    payload: dict[str, object] = response.json()
    payload["status_code"] = response.status_code
    return payload


def poll(client: TestClient, job_id: str, client_id: UUID) -> dict[str, object]:
    response = client.get(f"{GENERATIONS_ENDPOINT}/{job_id}", headers={"X-Client-Id": str(client_id)})
    assert response.status_code == HTTPStatus.OK
    body: dict[str, object] = response.json()
    assert spec_errors("GenerationJob", body) == []
    return body


@contextmanager
def generation_api(settings: Settings, cleanup: Cleanup, client_id: UUID) -> Iterator[TestClient]:
    cleanup.client_ids.add(client_id)
    with app_client(settings) as client:
        yield client


@asynccontextmanager
async def scripted_worker(settings: Settings, llm: FakeLlmClient) -> AsyncIterator[RunGeneration]:
    ctx: WorkerContext = {SETTINGS_KEY: settings, OBSERVABILITY_KEY: fresh_observability()}
    await startup(ctx)
    try:
        wired = from_context(ctx, RUN_GENERATION_KEY, RunGeneration)
        yield replace(
            wired,
            cache=InMemoryResultCache(),
            generator=LlmCardGenerator(llm=llm, new_id=uuid4, handlers=NOTE_TYPE_HANDLERS),
            moderator=LlmContentModerator(llm=FakeLlmClient(model_reply(EVERY_NOTE_ALLOWED))),
            documents=replace(wired.documents, moderator=LlmMaterialModerator(llm=llm)),
        )
    finally:
        await shutdown(ctx)


async def test_worker_wires_document_generation_with_the_configured_limits(settings: Settings) -> None:
    async with scripted_worker(settings, FakeLlmClient(model_reply(ALLOW))) as run:
        parser = run.documents.parser

    assert isinstance(parser, DocumentTextParser)
    assert parser.max_characters == settings.limits.document_max_source_characters
    assert parser.deadline_seconds == settings.limits.document_parse_deadline_seconds


async def test_document_generation_runs_the_real_pipeline_and_polls_like_a_topic_job(
    settings: Settings, documents: DocumentCleanup, cleanup: Cleanup
) -> None:
    client_id = documents.client_id()
    llm = FakeLlmClient(model_reply(ALLOW), model_reply(CARD_REPLY))
    with generation_api(settings, cleanup, client_id) as client:
        uploaded = upload(client, client_id, text_pdf(TOPIC_TEXT), filename="signs.pdf")
        created = start_generation(
            client, client_id, {"documentId": uploaded["documentId"], "language": "en", "cardCount": 5}
        )
        job_id = str(created["jobId"])
        cleanup.job_ids.add(UUID(job_id))
        queued = poll(client, job_id, client_id)
        async with scripted_worker(settings, llm) as run:
            await run(UUID(job_id))
        finished = poll(client, job_id, client_id)

    assert created["status_code"] == HTTPStatus.ACCEPTED, created
    assert queued["status"] == "queued"
    assert finished["status"] == "succeeded", finished
    result = finished["result"]
    assert isinstance(result, dict)
    [note] = result["notes"]
    assert note["sources"] == [
        {
            "kind": "document",
            "title": "signs.pdf",
            "documentId": uploaded["documentId"],
            "unit": "page",
            "number": 1,
        }
    ]
    screening, generation = llm.prompts
    assert "A red triangle warns of danger ahead." in screening.user
    assert "A red triangle warns of danger ahead." in generation.user
    row = await stored_row(documents.session_factory, UUID(str(uploaded["documentId"])))
    assert row is not None
    assert row.used_at is not None


async def test_document_generation_is_cancelled_like_a_topic_job_and_never_reads_the_document(
    settings: Settings, documents: DocumentCleanup, cleanup: Cleanup
) -> None:
    client_id = documents.client_id()
    llm = FakeLlmClient(model_reply(ALLOW), model_reply(CARD_REPLY))
    with generation_api(settings, cleanup, client_id) as client:
        uploaded = upload(client, client_id, text_pdf(TOPIC_TEXT))
        created = start_generation(
            client, client_id, {"documentId": uploaded["documentId"], "language": "en", "cardCount": 5}
        )
        job_id = str(created["jobId"])
        cleanup.job_ids.add(UUID(job_id))
        cancel = client.post(
            f"{GENERATIONS_ENDPOINT}/{job_id}/cancel", headers={"X-Client-Id": str(client_id)}
        )
        async with scripted_worker(settings, llm) as run:
            await run(UUID(job_id))
        finished = poll(client, job_id, client_id)

    assert cancel.status_code == HTTPStatus.NO_CONTENT
    assert finished["status"] == "cancelled"
    assert llm.prompts == []


@pytest.mark.parametrize("owner", ["another client", "nobody"])
async def test_generation_from_a_document_the_client_cannot_use_is_a_404_that_spends_no_quota(
    settings: Settings, documents: DocumentCleanup, cleanup: Cleanup, owner: str
) -> None:
    client_id = documents.client_id()
    with generation_api(settings, cleanup, client_id) as client:
        uploader = documents.client_id()
        foreign = (
            upload(client, uploader, pdf_bytes())["documentId"] if owner == "another client" else uuid4()
        )
        refused = start_generation(
            client, client_id, {"documentId": str(foreign), "language": "en", "cardCount": 5}
        )
        accepted = start_generation(
            client, client_id, {"topic": "Road signs", "language": "en", "cardCount": 5}
        )
        cleanup.job_ids.add(UUID(str(accepted["jobId"])))

    assert refused["status_code"] == HTTPStatus.NOT_FOUND
    assert refused["code"] == "DOCUMENT_NOT_FOUND"
    assert (
        spec_errors("Problem", {key: value for key, value in refused.items() if key != "status_code"}) == []
    )
    quota = accepted["quota"]
    assert isinstance(quota, dict)
    assert quota["remaining"] == quota["limit"] - 1


RECORDINGS_ENDPOINT = f"{API_PREFIX}/recordings"
REAL_RECORDINGS = [
    ("tone.mp3", DocumentFormat.MP3),
    ("tone.m4a", DocumentFormat.M4A),
    ("apple.m4a", DocumentFormat.M4A),
    ("tone.wav", DocumentFormat.WAV),
    ("clip.mp4", DocumentFormat.MP4),
    ("clip.mov", DocumentFormat.MOV),
    ("apple.mov", DocumentFormat.MOV),
]
VIDEOS = ["clip.mp4", "clip.mov", "apple.mov"]
EXTRACTION_CAP_SECONDS = 3


def upload_recording(client: TestClient, client_id: UUID, name: str) -> dict[str, object]:
    response = client.post(
        RECORDINGS_ENDPOINT,
        files={"file": (name, fixture(name), "application/octet-stream")},
        headers={"X-Client-Id": str(client_id)},
    )
    body: dict[str, object] = response.json()
    body["status"] = response.status_code
    return body


def with_recording_cap(settings: Settings, seconds: int) -> Settings:
    return replace(
        settings, limits=settings.limits.model_copy(update={"recording_max_duration_seconds": seconds})
    )


def ancient_recording(client_id: UUID, uploaded_at: datetime) -> Document:
    return replace(
        ancient_document(client_id, uploaded_at),
        filename="memo.m4a",
        size_bytes=len(fixture("tone.m4a")),
        document_format=DocumentFormat.M4A,
        duration_seconds=2.064,
    )


async def placed_recording(
    store: PostgresDocumentStore, storage: StorageProbe, recording: Document
) -> Document:
    await store.add_pending(recording)
    await storage.put(recording.storage_key, fixture("tone.m4a"), recording.document_format)
    await store.mark_stored(recording.document_id)
    return recording


@pytest.mark.parametrize(("name", "content_type"), REAL_RECORDINGS)
async def test_uploaded_recording_has_a_stored_record_with_its_duration_and_its_object_and_reads_back(
    settings: Settings, documents: DocumentCleanup, name: str, content_type: DocumentFormat
) -> None:
    client_id = documents.client_id()
    with app_client(settings) as client:
        body = upload_recording(client, client_id, name)
        read = client.get(f"{ENDPOINT}/{body['documentId']}", headers={"X-Client-Id": str(client_id)})

    assert body.pop("status") == HTTPStatus.CREATED, body
    assert spec_errors("Document", body) == []
    assert body["contentType"] == content_type
    row = await stored_row(documents.session_factory, UUID(str(body["documentId"])))
    assert row is not None
    assert row.status == DocumentStatus.STORED
    assert row.content_type == content_type
    assert row.duration_seconds == body["durationSeconds"]
    assert await documents.bucket.read(row.storage_key) == fixture(name)
    assert read.status_code == HTTPStatus.OK
    assert read.json() == body


async def test_recording_over_the_configured_cap_is_refused_before_anything_is_stored(
    settings: Settings, documents: DocumentCleanup
) -> None:
    client_id = documents.client_id()
    with app_client(with_recording_cap(settings, 1)) as client:
        body = upload_recording(client, client_id, "tone.m4a")

    assert body["status"] == HTTPStatus.UNPROCESSABLE_ENTITY
    assert body["code"] == "RECORDING_TOO_LONG"
    assert await rows_of(documents.session_factory, client_id) == []


async def test_document_disguised_as_a_recording_is_refused_by_its_content(
    settings: Settings, documents: DocumentCleanup
) -> None:
    client_id = documents.client_id()
    with app_client(settings) as client:
        response = client.post(
            RECORDINGS_ENDPOINT,
            files={"file": ("memo.mp3", pdf_bytes(), "audio/mpeg")},
            headers={"X-Client-Id": str(client_id)},
        )

    assert response.status_code == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert response.json()["code"] == "UNSUPPORTED_DOCUMENT_TYPE"
    assert await rows_of(documents.session_factory, client_id) == []


async def test_recording_gate_reports_its_own_limit_on_the_metrics_endpoint(settings: Settings) -> None:
    with app_client(settings) as client:
        scraped = client.get("/metrics").text

    assert f'deckly_gate_limit{{gate="{RECORDING_UPLOAD_GATE}"}} 2.0' in scraped
    assert f'deckly_gate_limit{{gate="{DOCUMENT_UPLOAD_GATE}"}} 4.0' in scraped


async def test_sweep_deletes_old_unused_recordings_exactly_like_documents(
    documents: DocumentCleanup, probe: StorageProbe
) -> None:
    session_factory, bucket = documents.session_factory, documents.bucket
    store = document_store(session_factory)
    client_id = documents.client_id()
    expired = await placed_recording(store, probe, ancient_recording(client_id, CUTOFF - TICK))
    recent = await placed_recording(store, probe, ancient_recording(client_id, CUTOFF))
    used = await placed_recording(store, probe, ancient_recording(client_id, ANCIENT))
    await generate_from(session_factory, used)

    purged = await sweeper(session_factory, probe)()

    assert purged >= 1
    remaining = {row.document_id for row in await rows_of(session_factory, client_id)}
    assert remaining == {recent.document_id, used.document_id}
    assert not await bucket.holds(expired.storage_key)
    assert await bucket.holds(recent.storage_key)
    assert await bucket.holds(used.storage_key)
    assert await store.get(recent.document_id, client_id) == recent


async def test_generation_from_a_recording_is_a_422_that_spends_no_quota_and_leaves_no_job(
    settings: Settings, documents: DocumentCleanup, cleanup: Cleanup
) -> None:
    client_id = documents.client_id()
    with generation_api(settings, cleanup, client_id) as client:
        recording = upload_recording(client, client_id, "tone.m4a")
        refused = start_generation(
            client, client_id, {"documentId": str(recording["documentId"]), "language": "en", "cardCount": 5}
        )
        accepted = start_generation(
            client, client_id, {"topic": "Road signs", "language": "en", "cardCount": 5}
        )
        cleanup.job_ids.add(UUID(str(accepted["jobId"])))

    assert refused["status_code"] == HTTPStatus.UNPROCESSABLE_ENTITY
    assert refused["code"] == "DOCUMENT_NOT_SUPPORTED_FOR_GENERATION"
    assert (
        spec_errors("Problem", {key: value for key, value in refused.items() if key != "status_code"}) == []
    )
    assert await jobs_referencing(documents.session_factory, UUID(str(recording["documentId"]))) == []
    quota = accepted["quota"]
    assert isinstance(quota, dict)
    assert quota["remaining"] == quota["limit"] - 1


@pytest.mark.parametrize("name", VIDEOS)
async def test_stored_video_yields_its_audio_through_the_hardened_ffmpeg_extraction(
    settings: Settings, documents: DocumentCleanup, tmp_path: Path, name: str
) -> None:
    client_id = documents.client_id()
    with app_client(settings) as client:
        body = upload_recording(client, client_id, name)
    row = await stored_row(documents.session_factory, UUID(str(body["documentId"])))
    assert row is not None
    source, output = tmp_path / "stored", tmp_path / "audio.wav"
    source.write_bytes(await documents.bucket.read(row.storage_key))

    await extract_audio(source, DocumentFormat(row.content_type), EXTRACTION_CAP_SECONDS, output)

    extracted = detect_recording(output.read_bytes(), EXTRACTION_CAP_SECONDS)
    assert extracted.document_format is DocumentFormat.WAV
    assert extracted.duration_seconds == pytest.approx(2.0, abs=0.1)


async def test_extraction_stops_at_the_cap_whatever_the_recording_holds(tmp_path: Path) -> None:
    source, output = tmp_path / "long.wav", tmp_path / "audio.wav"
    source.write_bytes(pcm_wave(seconds=5))

    await extract_audio(source, DocumentFormat.WAV, EXTRACTION_CAP_SECONDS, output)

    assert detect_recording(output.read_bytes(), EXTRACTION_CAP_SECONDS).duration_seconds == pytest.approx(
        EXTRACTION_CAP_SECONDS, abs=0.01
    )
