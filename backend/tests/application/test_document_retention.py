import asyncio
import logging
from datetime import timedelta

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.housekeeping import DocumentRetentionPolicy, EnforceDocumentRetention
from deckly.infrastructure.documents.store import DocumentStatus
from tests.documents import InMemoryDocumentStorage, InMemoryDocumentStore, document
from tests.domain.builders import T0

pytestmark = pytest.mark.anyio

RETENTION = timedelta(hours=24)
NOW = T0 + timedelta(days=3)
CUTOFF = NOW - RETENTION
TICK = timedelta(microseconds=1)
BATCH_SIZE = 10
MAX_CONCURRENCY = 2


class Bucket(InMemoryDocumentStorage):
    def __init__(self, store: InMemoryDocumentStore) -> None:
        super().__init__()
        for row in store.rows.values():
            self.objects[row.document.storage_key] = (b"%PDF-1.7 %%EOF", row.document.document_format)


def retention(
    store: InMemoryDocumentStore, storage: InMemoryDocumentStorage, batch_size: int = BATCH_SIZE
) -> EnforceDocumentRetention:
    return EnforceDocumentRetention(
        housekeeping=store,
        storage=storage,
        policy=DocumentRetentionPolicy(
            retention=RETENTION, batch_size=batch_size, max_concurrency=MAX_CONCURRENCY
        ),
        clock=lambda: NOW,
    )


async def test_old_unused_document_loses_both_its_object_and_its_record() -> None:
    store = InMemoryDocumentStore()
    store.keep(document(1, uploaded_at=CUTOFF - TICK))
    storage = Bucket(store)

    purged = await retention(store, storage)()

    assert purged == 1
    assert store.rows == {}
    assert storage.objects == {}


async def test_document_uploaded_exactly_at_the_cutoff_is_kept() -> None:
    store = InMemoryDocumentStore()
    store.keep(document(1, uploaded_at=CUTOFF))
    storage = Bucket(store)

    assert await retention(store, storage)() == 0
    assert len(store.rows) == 1
    assert len(storage.objects) == 1


async def test_recent_document_is_kept() -> None:
    store = InMemoryDocumentStore()
    store.keep(document(1, uploaded_at=NOW - timedelta(hours=1)))
    storage = Bucket(store)

    assert await retention(store, storage)() == 0
    assert storage.deleted == []


async def test_old_document_marked_as_used_is_never_swept() -> None:
    store = InMemoryDocumentStore()
    used = document(1, uploaded_at=CUTOFF - timedelta(days=30))
    store.keep(used)
    store.mark_used(used.document_id, at=CUTOFF - timedelta(days=29))
    storage = Bucket(store)

    assert await retention(store, storage)() == 0
    assert used.document_id in store.rows
    assert storage.deleted == []


async def test_old_pending_upload_is_swept_whether_or_not_its_object_was_ever_stored() -> None:
    store = InMemoryDocumentStore()
    store.keep(document(1, uploaded_at=CUTOFF - TICK), DocumentStatus.PENDING)
    storage = InMemoryDocumentStorage()

    assert await retention(store, storage)() == 1
    assert store.rows == {}
    assert storage.deleted == [document(1).storage_key]


async def test_record_whose_object_could_not_be_deleted_is_kept_for_the_next_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryDocumentStore()
    stuck, gone = document(1, uploaded_at=CUTOFF - TICK), document(2, uploaded_at=CUTOFF - TICK)
    store.keep(stuck)
    store.keep(gone)
    storage = Bucket(store)
    storage.undeletable[stuck.storage_key] = UpstreamUnavailableError(5)

    with caplog.at_level(logging.WARNING):
        purged = await retention(store, storage)()

    assert purged == 1
    assert store.rows.keys() == {stuck.document_id}
    assert storage.objects.keys() == {stuck.storage_key}
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == [
        "document_object_not_deleted"
    ]


async def test_unexpected_storage_failure_is_logged_with_its_traceback_and_keeps_the_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryDocumentStore()
    stuck = document(1, uploaded_at=CUTOFF - TICK)
    store.keep(stuck)
    storage = Bucket(store)
    storage.undeletable[stuck.storage_key] = RuntimeError("bug")

    with caplog.at_level(logging.ERROR):
        assert await retention(store, storage)() == 0

    assert stuck.document_id in store.rows
    [record] = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert record.exc_info is not None


async def test_one_run_sweeps_at_most_one_batch_oldest_first() -> None:
    store = InMemoryDocumentStore()
    for number in range(1, 6):
        store.keep(document(number, uploaded_at=CUTOFF - timedelta(hours=number)))
    storage = Bucket(store)

    assert await retention(store, storage, batch_size=2)() == 2
    assert {row.document.document_id for row in store.rows.values()} == {
        document(number).document_id for number in (1, 2, 3)
    }


class GatedStorage(InMemoryDocumentStorage):
    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.most_in_flight = 0

    async def delete(self, key: str) -> None:
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.in_flight -= 1
        await super().delete(key)


async def test_object_deletions_never_exceed_the_concurrency_cap() -> None:
    store = InMemoryDocumentStore()
    for number in range(1, 8):
        store.keep(document(number, uploaded_at=CUTOFF - TICK))
    storage = GatedStorage()

    assert await retention(store, storage)() == 7
    assert storage.most_in_flight == MAX_CONCURRENCY


async def test_run_with_nothing_expired_purges_nothing() -> None:
    store = InMemoryDocumentStore()

    assert await retention(store, InMemoryDocumentStorage())() == 0
    [claim] = store.claims
    assert claim.purged == []
