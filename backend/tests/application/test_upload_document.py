import asyncio
import logging

import pytest

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import (
    DocumentNotFoundError,
    DocumentTooLargeError,
    InvalidDocumentError,
    UnsupportedDocumentError,
)
from deckly.infrastructure.documents.store import DocumentStatus
from tests.documents import (
    CLIENT_ID,
    MAX_TEST_DOCUMENT_BYTES,
    OTHER_CLIENT_ID,
    DocumentHarness,
    LosesPutResponse,
    office_bytes,
    padded_pdf,
    pdf_bytes,
    pptx_bytes,
    storage_outage,
)
from tests.domain.builders import T0

pytestmark = pytest.mark.anyio


async def test_uploaded_pdf_is_stored_recorded_and_readable_by_its_client() -> None:
    harness = DocumentHarness()
    content = pdf_bytes()

    stored = await harness.send(content, filename="lecture.pdf")

    assert stored.document_format is DocumentFormat.PDF
    assert stored.size_bytes == len(content)
    assert stored.uploaded_at == T0
    assert harness.storage.objects == {stored.storage_key: (content, DocumentFormat.PDF)}
    assert harness.store.rows[stored.document_id].status is DocumentStatus.STORED
    assert await harness.get(stored.document_id, CLIENT_ID) == stored


async def test_pptx_is_recognised_by_its_content_whatever_its_name() -> None:
    harness = DocumentHarness()

    stored = await harness.send(pptx_bytes(), filename="slides.pdf")

    assert stored.document_format is DocumentFormat.PPTX
    assert stored.filename == "slides.pdf"


async def test_rate_limited_client_is_refused_before_the_body_is_read() -> None:
    harness = DocumentHarness()
    harness.limiter.limit = 0

    with pytest.raises(RateLimitedError):
        await harness.send(pdf_bytes())

    assert harness.reads == 0
    assert harness.store.rows == {}
    assert harness.storage.objects == {}


async def test_unreachable_limiter_refuses_the_upload_instead_of_admitting_it_unmetered() -> None:
    harness = DocumentHarness()
    harness.limiter.failure = UpstreamUnavailableError(1)

    with pytest.raises(UpstreamUnavailableError):
        await harness.send(pdf_bytes())

    assert harness.reads == 0
    assert harness.storage.objects == {}


async def test_upload_exactly_at_the_size_limit_is_accepted() -> None:
    harness = DocumentHarness()

    stored = await harness.send(padded_pdf(MAX_TEST_DOCUMENT_BYTES))

    assert stored.size_bytes == MAX_TEST_DOCUMENT_BYTES


async def test_upload_one_byte_over_the_size_limit_is_refused_before_anything_is_stored() -> None:
    harness = DocumentHarness()

    with pytest.raises(DocumentTooLargeError):
        await harness.send(padded_pdf(MAX_TEST_DOCUMENT_BYTES + 1))

    assert harness.store.rows == {}
    assert harness.storage.objects == {}


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"just some text with a .pdf name",
        b"%PDF-1.7 but never finished",
        office_bytes(
            main_part="word/document.xml",
            main_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        ),
    ],
    ids=["empty", "text", "truncated pdf", "docx"],
)
async def test_content_that_is_not_a_pdf_or_pptx_is_refused_before_anything_is_stored(content: bytes) -> None:
    harness = DocumentHarness()

    with pytest.raises(UnsupportedDocumentError):
        await harness.send(content, filename="lecture.pdf")

    assert harness.store.rows == {}
    assert harness.storage.objects == {}


async def test_upload_whose_filename_has_nothing_visible_is_refused_before_anything_is_stored() -> None:
    harness = DocumentHarness()

    with pytest.raises(InvalidDocumentError):
        await harness.send(pdf_bytes(), filename="\u200b")

    assert harness.store.rows == {}
    assert harness.storage.objects == {}


async def test_storage_outage_leaves_neither_a_record_nor_an_object() -> None:
    harness = DocumentHarness()
    harness.storage.put_failure = storage_outage()

    with pytest.raises(UpstreamUnavailableError):
        await harness.send(pdf_bytes())

    assert harness.store.rows == {}
    assert harness.storage.objects == {}


async def test_storage_outage_is_reported_even_when_the_pending_record_cannot_be_discarded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = DocumentHarness()
    harness.storage.put_failure = storage_outage()
    harness.store.failures["discard"] = ConnectionError("database unreachable")

    with caplog.at_level(logging.ERROR), pytest.raises(UpstreamUnavailableError):
        await harness.send(pdf_bytes())

    [row] = harness.store.rows.values()
    assert row.status is DocumentStatus.PENDING
    assert [record.getMessage() for record in caplog.records] == ["document_not_discarded"]


async def test_cancelled_upload_discards_its_pending_record() -> None:
    harness = DocumentHarness()
    harness.storage.put_failure = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await harness.send(pdf_bytes())

    assert harness.store.rows == {}


async def test_record_that_cannot_be_written_stops_the_upload_before_storage() -> None:
    harness = DocumentHarness()
    harness.store.failures["add_pending"] = TimeoutError("database unresponsive")

    with pytest.raises(TimeoutError):
        await harness.send(pdf_bytes())

    assert harness.storage.objects == {}


async def test_object_stored_but_not_confirmed_stays_pending_and_invisible_for_the_sweep() -> None:
    harness = DocumentHarness()
    harness.store.failures["mark_stored"] = TimeoutError("database unresponsive")

    with pytest.raises(TimeoutError):
        await harness.send(pdf_bytes())

    [row] = harness.store.rows.values()
    assert row.status is DocumentStatus.PENDING
    assert harness.storage.objects.keys() == {row.document.storage_key}
    with pytest.raises(DocumentNotFoundError):
        await harness.get(row.document.document_id, CLIENT_ID)


async def test_document_is_invisible_to_any_other_client() -> None:
    harness = DocumentHarness()
    stored = await harness.send(pdf_bytes())

    with pytest.raises(DocumentNotFoundError):
        await harness.get(stored.document_id, OTHER_CLIENT_ID)


async def test_every_upload_gets_its_own_document_even_with_identical_content() -> None:
    harness = DocumentHarness()

    first = await harness.send(pdf_bytes())
    second = await harness.send(pdf_bytes())

    assert first.document_id != second.document_id
    assert len(harness.storage.objects) == 2


async def test_upload_whose_storage_answer_was_lost_never_leaves_an_object_without_a_record() -> None:
    harness = DocumentHarness(storage=LosesPutResponse())

    with pytest.raises(UpstreamUnavailableError):
        await harness.send(pdf_bytes())

    recorded = {row.document.storage_key for row in harness.store.rows.values()}
    assert harness.storage.objects.keys() <= recorded


async def test_failed_upload_whose_object_cannot_be_removed_keeps_its_invisible_record_for_the_sweep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = DocumentHarness(storage=LosesPutResponse())
    harness.storage.undeletable["documents/00000000-0000-4000-8000-000000000001"] = storage_outage()

    with caplog.at_level(logging.WARNING), pytest.raises(UpstreamUnavailableError):
        await harness.send(pdf_bytes())

    [row] = harness.store.rows.values()
    assert row.status is DocumentStatus.PENDING
    assert harness.storage.objects.keys() == {row.document.storage_key}
    with pytest.raises(DocumentNotFoundError):
        await harness.get(row.document.document_id, CLIENT_ID)
    assert "document_left_for_sweep" in [record.getMessage() for record in caplog.records]


async def test_unexpected_failure_removing_the_object_is_logged_with_its_traceback_and_keeps_the_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = DocumentHarness(storage=LosesPutResponse())
    harness.storage.undeletable["documents/00000000-0000-4000-8000-000000000001"] = RuntimeError("bug")

    with caplog.at_level(logging.ERROR), pytest.raises(UpstreamUnavailableError):
        await harness.send(pdf_bytes())

    assert len(harness.store.rows) == 1
    [record] = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert record.getMessage() == "document_left_for_sweep"
    assert record.exc_info is not None
