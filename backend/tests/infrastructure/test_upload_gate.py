import asyncio
from uuid import UUID

import pytest

from deckly.application.exceptions import UploadCapacityExceededError, UpstreamUnavailableError
from deckly.domain.exceptions import UnsupportedDocumentError
from deckly.infrastructure.observability.metrics import DOCUMENT_UPLOAD_GATE, GateWait, Metrics
from deckly.infrastructure.upload_gate import UPLOAD_CAPACITY_RETRY_AFTER_SECONDS
from tests.documents import DocumentHarness, InMemoryDocumentStorage, pdf_bytes, storage_outage

pytestmark = pytest.mark.anyio

CAP = 5
BURST = 40
FAIL_FAST_SECONDS = 1


def client_of(number: int) -> UUID:
    return UUID(int=number + 1)


def in_flight(metrics: Metrics) -> float | None:
    return metrics.registry.get_sample_value("deckly_gate_in_flight", {"gate": DOCUMENT_UPLOAD_GATE})


def rejections(metrics: Metrics) -> float | None:
    return metrics.registry.get_sample_value("deckly_gate_rejections_total", {"gate": DOCUMENT_UPLOAD_GATE})


def waits(metrics: Metrics, suffix: str, outcome: GateWait = GateWait.ACQUIRED) -> float | None:
    return metrics.registry.get_sample_value(
        f"deckly_gate_wait_seconds_{suffix}", {"gate": DOCUMENT_UPLOAD_GATE, "outcome": outcome}
    )


class SlowStorage(InMemoryDocumentStorage):
    def __init__(self, *, parties: int = 1) -> None:
        super().__init__()
        self.metrics: Metrics | None = None
        self.parties = parties
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        self.in_flight = 0
        self.peak = 0
        self.gauge_peak = 0.0

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.metrics is not None:
            self.gauge_peak = max(self.gauge_peak, in_flight(self.metrics) or 0.0)
        try:
            if self.in_flight >= self.parties:
                self.arrived.set()
            await self.release.wait()
            await super().put(key, content, content_type)
        finally:
            self.in_flight -= 1


async def holds_every_slot(gate: asyncio.Semaphore, limit: int) -> bool:
    try:
        async with asyncio.timeout(FAIL_FAST_SECONDS):
            for _ in range(limit):
                await gate.acquire()
    except TimeoutError:
        return False
    return gate.locked()


def slow_harness(cap: int, *, parties: int = 1) -> tuple[DocumentHarness, SlowStorage]:
    storage = SlowStorage(parties=parties)
    harness = DocumentHarness(storage=storage, concurrency=cap)
    storage.metrics = harness.metrics
    return harness, storage


def start_uploads(harness: DocumentHarness, count: int) -> list["asyncio.Task[object]"]:
    return [asyncio.create_task(harness.send(pdf_bytes(), client_id=client_of(n))) for n in range(count)]


async def test_burst_of_uploads_never_puts_more_than_the_cap_in_flight_and_refuses_the_rest() -> None:
    harness, storage = slow_harness(CAP, parties=CAP)
    uploads = start_uploads(harness, BURST)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        await storage.arrived.wait()
        storage.release.set()
        outcomes = await asyncio.gather(*uploads, return_exceptions=True)

    stored = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, UploadCapacityExceededError)]
    assert storage.peak == CAP
    assert storage.gauge_peak == CAP
    assert len(stored) == CAP
    assert len(refused) == BURST - CAP
    assert len(harness.storage.objects) == CAP
    assert in_flight(harness.metrics) == 0
    assert rejections(harness.metrics) == BURST - CAP
    assert await holds_every_slot(harness.slots, CAP)


async def test_the_same_burst_without_a_cap_puts_every_upload_in_flight_at_once() -> None:
    harness, storage = slow_harness(BURST, parties=BURST)
    uploads = start_uploads(harness, BURST)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        await storage.arrived.wait()
        storage.release.set()
        await asyncio.gather(*uploads)

    assert storage.peak == BURST
    assert rejections(harness.metrics) is None


async def test_upload_over_the_cap_is_refused_at_once_with_a_retry_hint_instead_of_queued() -> None:
    harness, storage = slow_harness(1)
    holder = start_uploads(harness, 1)[0]
    await storage.arrived.wait()

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        with pytest.raises(UploadCapacityExceededError) as refused:
            await harness.send(pdf_bytes(), client_id=client_of(1))

    assert refused.value.retry_after_seconds == UPLOAD_CAPACITY_RETRY_AFTER_SECONDS
    assert isinstance(refused.value, UpstreamUnavailableError)
    storage.release.set()
    await holder


async def test_refused_upload_reads_no_body_stores_nothing_and_spends_none_of_the_clients_budget() -> None:
    harness, storage = slow_harness(1)
    holder = start_uploads(harness, 1)[0]
    await storage.arrived.wait()

    with pytest.raises(UploadCapacityExceededError):
        await harness.send(pdf_bytes(), client_id=client_of(1))

    assert harness.reads == 1
    assert harness.limiter.used[client_of(1)] == 0
    assert len(harness.store.rows) == 1
    storage.release.set()
    await holder
    assert (await harness.send(pdf_bytes(), client_id=client_of(1))).size_bytes == len(pdf_bytes())
    assert harness.limiter.used[client_of(1)] == 1


async def test_refused_upload_is_not_counted_as_in_flight_or_as_a_wait() -> None:
    harness, storage = slow_harness(1)
    holder = start_uploads(harness, 1)[0]
    await storage.arrived.wait()

    with pytest.raises(UploadCapacityExceededError):
        await harness.send(pdf_bytes(), client_id=client_of(1))

    assert in_flight(harness.metrics) == 1
    assert waits(harness.metrics, "count") == 1
    assert rejections(harness.metrics) == 1
    storage.release.set()
    await holder


async def test_slot_is_given_back_when_an_upload_fails() -> None:
    harness = DocumentHarness(concurrency=1)
    harness.storage.put_failure = storage_outage()

    with pytest.raises(UpstreamUnavailableError):
        await harness.send(pdf_bytes())
    harness.storage.put_failure = None
    stored = await harness.send(pdf_bytes())

    assert stored.size_bytes == len(pdf_bytes())
    assert in_flight(harness.metrics) == 0
    assert await holds_every_slot(harness.slots, 1)


async def test_slot_is_given_back_when_an_upload_is_cancelled() -> None:
    harness, storage = slow_harness(1)
    holder = start_uploads(harness, 1)[0]
    await storage.arrived.wait()

    holder.cancel()
    await asyncio.gather(holder, return_exceptions=True)
    storage.release.set()

    assert in_flight(harness.metrics) == 0
    assert (await harness.send(pdf_bytes(), client_id=client_of(1))).size_bytes == len(pdf_bytes())
    assert await holds_every_slot(harness.slots, 1)


async def test_slot_is_given_back_when_an_upload_is_refused_for_its_content() -> None:
    harness = DocumentHarness(concurrency=1)

    with pytest.raises(UnsupportedDocumentError):
        await harness.send(b"not a document")
    stored = await harness.send(pdf_bytes())

    assert stored.size_bytes == len(pdf_bytes())
    assert await holds_every_slot(harness.slots, 1)


async def test_uploads_within_the_cap_all_run_together_and_none_is_refused() -> None:
    harness, storage = slow_harness(CAP, parties=CAP)
    uploads = start_uploads(harness, CAP)

    async with asyncio.timeout(FAIL_FAST_SECONDS):
        await storage.arrived.wait()
        assert in_flight(harness.metrics) == CAP
        storage.release.set()
        await asyncio.gather(*uploads)

    assert storage.peak == CAP
    assert rejections(harness.metrics) is None


async def test_sequential_uploads_under_a_cap_of_one_are_never_refused() -> None:
    harness = DocumentHarness(concurrency=1)

    stored = [await harness.send(pdf_bytes(), client_id=client_of(n)) for n in range(CAP)]

    assert len(stored) == CAP
    assert rejections(harness.metrics) is None
    assert waits(harness.metrics, "count") == CAP
    assert waits(harness.metrics, "sum") == 0
    assert waits(harness.metrics, "count", GateWait.ABANDONED) is None
    assert in_flight(harness.metrics) == 0
