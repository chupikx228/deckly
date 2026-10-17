import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from deckly.application.documents import FetchRecording
from deckly.application.exceptions import RateLimitedError, UploadCapacityExceededError
from deckly.application.ports import Requester
from deckly.domain.document import Document, DocumentFormat
from deckly.domain.exceptions import (
    RecordingTooLongError,
    UnsupportedDocumentError,
    UnsupportedRecordingUrlError,
)
from deckly.infrastructure.documents.store import DocumentStatus
from deckly.infrastructure.observability.metrics import RECORDING_URL_GATE
from deckly.infrastructure.recordings.sniffing import ContentRecordingInspector
from deckly.infrastructure.upload_gate import RejectingUploadGate
from tests.documents import ADDRESS, CLIENT_ID, DocumentHarness
from tests.fake_ytdlp import TEST_MAX_SECONDS, FakeYtDlp, fake_fetcher, video_info
from tests.recording_files import (
    AAC_FRAME_SAMPLES,
    FIXTURES,
    SOUND_TIMESCALE,
    Track,
    WaveFormat,
    ape_tag,
    cd_frame,
    chunk,
    fragment_header,
    fragmented_movie,
    frames_for,
    isobmff,
    movie,
    movie_fragment,
    mp3,
    sample_sizes,
    sampled_count,
    time_to_sample,
    track_fragment,
    track_run,
    wave,
)

pytestmark = pytest.mark.anyio

URL = "https://youtu.be/jNQXAC9IVRw?si=shared"
MAX_BYTES = 4 * 1024 * 1024
REQUESTER = Requester(client_id=CLIENT_ID, address=ADDRESS)


class FetchHarness:
    def __init__(self, workspace: Path, probe: dict[str, object], download: dict[str, object]) -> None:
        self.documents = DocumentHarness(max_bytes=MAX_BYTES, concurrency=1)
        self.fake = FakeYtDlp(workspace, probe, download)
        self.fetch = FetchRecording(
            upload=replace(
                self.documents.upload,
                gate=RejectingUploadGate(self.documents.slots, RECORDING_URL_GATE, self.documents.metrics),
                inspector=ContentRecordingInspector(max_duration_seconds=TEST_MAX_SECONDS),
            ),
            fetcher=fake_fetcher(self.fake, metrics=self.documents.metrics, max_bytes=MAX_BYTES),
        )

    async def send(self, url: str = URL) -> Document:
        return await self.fetch(REQUESTER, url)

    def assert_nothing_stored(self) -> None:
        assert self.documents.store.rows == {}
        assert self.documents.storage.objects == {}


def downloading(workspace: Path, content: bytes) -> dict[str, object]:
    recording = workspace / "served-recording"
    recording.write_bytes(content)
    return {"file": str(recording)}


def harness_serving(workspace: Path, content: bytes, **info: object) -> FetchHarness:
    return FetchHarness(workspace, {"info": video_info(**info)}, downloading(workspace, content))


async def test_supported_link_is_fetched_validated_and_stored_exactly_like_an_upload(tmp_path: Path) -> None:
    content = (FIXTURES / "tone-fragmented.m4a").read_bytes()
    harness = harness_serving(tmp_path, content, duration=2.0)

    stored = await harness.send()

    assert stored.document_format is DocumentFormat.M4A
    assert stored.duration_seconds == pytest.approx(2.064)
    assert stored.filename == "Lecture 1: Introduction.m4a"
    assert stored.size_bytes == len(content)
    assert stored.client_id == CLIENT_ID
    assert harness.documents.storage.objects == {stored.storage_key: (content, DocumentFormat.M4A)}
    assert harness.documents.store.rows[stored.document_id].status is DocumentStatus.STORED
    assert await harness.documents.get(stored.document_id, CLIENT_ID) == stored
    assert harness.documents.limiter.used[CLIENT_ID] == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456",
        "https://www.youtube.com/playlist?list=PL123",
        "http://169.254.169.254/latest/meta-data/",
        "https://www.youtube.com.evil.example/watch?v=jNQXAC9IVRw",
        "file:///etc/passwd",
    ],
)
async def test_unsupported_link_is_refused_before_a_slot_the_budget_or_the_downloader_is_touched(
    tmp_path: Path, url: str
) -> None:
    harness = harness_serving(tmp_path, b"")

    with pytest.raises(UnsupportedRecordingUrlError):
        await harness.send(url)

    assert harness.documents.limiter.used[CLIENT_ID] == 0
    assert harness.fake.invocations == []
    assert not harness.documents.slots.locked()
    harness.assert_nothing_stored()


async def test_video_over_the_cap_by_its_metadata_is_refused_without_downloading_it(tmp_path: Path) -> None:
    harness = harness_serving(tmp_path, b"never served", duration=TEST_MAX_SECONDS * 10)

    with pytest.raises(RecordingTooLongError):
        await harness.send()

    assert len(harness.fake.invocations) == 1
    assert harness.documents.limiter.used[CLIENT_ID] == 1
    harness.assert_nothing_stored()


def long_fragmented_recording(seconds: int) -> bytes:
    frames = seconds * SOUND_TIMESCALE // AAC_FRAME_SAMPLES
    return fragmented_movie(
        movie_fragment(track_fragment(fragment_header(), track_run(frames))), header_seconds=10
    )


async def test_download_longer_than_its_metadata_claimed_is_caught_by_the_probe_and_not_stored(
    tmp_path: Path,
) -> None:
    harness = harness_serving(tmp_path, long_fragmented_recording(TEST_MAX_SECONDS * 2), duration=10.0)

    with pytest.raises(RecordingTooLongError):
        await harness.send()

    assert len(harness.fake.invocations) == 2
    harness.assert_nothing_stored()


def hidden_trailing_tag_mp3() -> bytes:
    return mp3(frames=40) + ape_tag(items=cd_frame() * 2000)


def understated_sample_table_m4a() -> bytes:
    table = time_to_sample(frames_for(2))
    return isobmff(movie(Track(stts=table, sizes=sample_sizes(sampled_count(table) * 1000)).encode()))


def second_format_chunk_wave() -> bytes:
    fast = WaveFormat(sample_rate=384_000, channels=8, bits=32).encode()
    return wave(
        chunk(b"fmt ", WaveFormat().encode()), chunk(b"fmt ", fast), chunk(b"data", b"\x00\x01" * 32_000)
    )


def one_tick_aac_fragments() -> bytes:
    run = track_run(20_000)
    return fragmented_movie(movie_fragment(track_fragment(fragment_header(default_duration=1), run)))


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (hidden_trailing_tag_mp3(), UnsupportedDocumentError),
        (understated_sample_table_m4a(), UnsupportedDocumentError),
        (second_format_chunk_wave(), UnsupportedDocumentError),
        (one_tick_aac_fragments(), RecordingTooLongError),
    ],
    ids=["mp3-frames-in-trailing-tag", "stts-fewer-than-stsz", "second-fmt-chunk", "aac-one-tick-fragments"],
)
async def test_downloaded_file_built_to_lie_about_its_length_is_refused_like_an_upload(
    tmp_path: Path, content: bytes, error: type[Exception]
) -> None:
    harness = harness_serving(tmp_path, content, duration=1.0, filesize=None)

    with pytest.raises(error):
        await harness.send()

    harness.assert_nothing_stored()


async def test_busy_fetch_slot_refuses_at_once_without_spending_the_budget(tmp_path: Path) -> None:
    harness = harness_serving(tmp_path, (FIXTURES / "tone-fragmented.m4a").read_bytes())
    await harness.documents.slots.acquire()

    with pytest.raises(UploadCapacityExceededError):
        await harness.send()

    assert harness.documents.limiter.used[CLIENT_ID] == 0
    assert harness.fake.invocations == []


async def test_spent_budget_refuses_before_anything_is_fetched(tmp_path: Path) -> None:
    harness = harness_serving(tmp_path, (FIXTURES / "tone-fragmented.m4a").read_bytes())
    harness.documents.limiter.limit = 0

    with pytest.raises(RateLimitedError):
        await harness.send()

    assert harness.fake.invocations == []
    harness.assert_nothing_stored()


async def test_one_fetch_at_a_time_with_one_slot(tmp_path: Path) -> None:
    content = (FIXTURES / "tone-fragmented.m4a").read_bytes()
    harness = FetchHarness(tmp_path, {"info": video_info(), "sleep": 1}, downloading(tmp_path, content))

    results = await asyncio.gather(harness.send(), harness.send(), return_exceptions=True)

    assert sorted(type(result).__name__ for result in results) == ["Document", "UploadCapacityExceededError"]
