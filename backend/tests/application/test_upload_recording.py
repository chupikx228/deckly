import pytest

from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import DocumentTooLargeError, RecordingTooLongError, UnsupportedDocumentError
from deckly.infrastructure.documents.store import DocumentStatus
from tests.documents import (
    CLIENT_ID,
    MAX_TEST_DOCUMENT_BYTES,
    pdf_bytes,
    recording_harness,
)
from tests.logs import captured_json_logs
from tests.recording_files import fixture, pcm_wave

pytestmark = pytest.mark.anyio

OVER_LIMIT_SECONDS = 5


@pytest.mark.parametrize(
    ("name", "document_format"),
    [
        ("tone.mp3", DocumentFormat.MP3),
        ("apple.m4a", DocumentFormat.M4A),
        ("tone.wav", DocumentFormat.WAV),
        ("clip.mp4", DocumentFormat.MP4),
        ("apple.mov", DocumentFormat.MOV),
    ],
)
async def test_recording_is_stored_with_its_detected_type_and_duration(
    name: str, document_format: DocumentFormat
) -> None:
    harness = recording_harness()
    content = fixture(name)

    with captured_json_logs() as logs:
        stored = await harness.send(content, filename=f"lecture.{name.rsplit('.', 1)[1]}")

    assert stored.document_format is document_format
    assert stored.duration_seconds is not None
    assert stored.duration_seconds >= 1
    assert harness.storage.objects == {stored.storage_key: (content, document_format)}
    assert harness.store.rows[stored.document_id].status is DocumentStatus.STORED
    assert await harness.get(stored.document_id, CLIENT_ID) == stored
    [uploaded] = logs.named("document_uploaded")
    assert uploaded["duration_seconds"] == stored.duration_seconds


async def test_recording_type_is_decided_by_content_not_by_its_name() -> None:
    stored = await recording_harness().send(fixture("tone.m4a"), filename="voice-memo.mp3")

    assert stored.document_format is DocumentFormat.M4A
    assert stored.filename == "voice-memo.mp3"


async def test_recording_over_the_duration_cap_is_refused_and_nothing_is_stored() -> None:
    harness = recording_harness(max_duration_seconds=1)

    with pytest.raises(RecordingTooLongError):
        await harness.send(pcm_wave(seconds=2), filename="long.wav")

    assert harness.store.rows == {}
    assert harness.storage.objects == {}
    assert harness.limiter.used[CLIENT_ID] == 1


async def test_recording_exactly_at_the_duration_cap_is_stored() -> None:
    stored = await recording_harness(max_duration_seconds=2).send(pcm_wave(seconds=2), filename="exact.wav")

    assert stored.duration_seconds == 2.0


async def test_document_sent_as_a_recording_is_refused_and_nothing_is_stored() -> None:
    harness = recording_harness()

    with pytest.raises(UnsupportedDocumentError):
        await harness.send(pdf_bytes(), filename="lecture.mp3")

    assert harness.store.rows == {}
    assert harness.storage.objects == {}


async def test_recording_over_the_size_limit_is_refused_before_it_is_probed() -> None:
    harness = recording_harness(max_duration_seconds=1)
    content = pcm_wave(seconds=OVER_LIMIT_SECONDS)
    assert len(content) > MAX_TEST_DOCUMENT_BYTES

    with pytest.raises(DocumentTooLargeError):
        await harness.send(content, filename="huge.wav")

    assert harness.store.rows == {}
    assert harness.storage.objects == {}
