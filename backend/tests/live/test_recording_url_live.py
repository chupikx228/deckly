import pytest

from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import RecordingTooLongError
from deckly.domain.recording_url import parse_youtube_video
from deckly.infrastructure.recording_url.fetcher import (
    YOUTUBE_EGRESS_POLICY,
    FetchLimits,
    FetchOutcome,
    build_youtube_fetcher,
)
from deckly.infrastructure.recordings.sniffing import detect_recording
from tests.fakes import fresh_metrics
from tests.logs import captured_json_logs

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SHORT_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
SHORT_VIDEO_SECONDS = 19
LONG_VIDEO = "https://www.youtube.com/watch?v=aircAruvnKk"
CAP_SECONDS = 15 * 60
LIMITS = FetchLimits(max_bytes=48 * 1024 * 1024, max_duration_seconds=CAP_SECONDS, timeout_seconds=120)


async def test_short_youtube_video_is_fetched_through_the_egress_proxy_and_probes_as_fragmented_m4a() -> None:
    metrics = fresh_metrics()
    fetcher = build_youtube_fetcher(LIMITS, metrics)

    with captured_json_logs() as logs:
        upload = await fetcher.fetch(parse_youtube_video(SHORT_VIDEO))

    inspected = detect_recording(upload.content, CAP_SECONDS)
    assert inspected.document_format is DocumentFormat.M4A
    assert inspected.duration_seconds is not None
    assert inspected.duration_seconds == pytest.approx(SHORT_VIDEO_SECONDS, abs=1.5)
    assert upload.filename.endswith(".m4a")
    [line] = logs.named("recording_url_hosts")
    hosts = line["hosts"]
    assert isinstance(hosts, list)
    assert hosts
    assert all(isinstance(host, str) and YOUTUBE_EGRESS_POLICY.allows(host) for host in hosts)
    assert (
        metrics.registry.get_sample_value(
            "deckly_recording_url_fetches_total", {"outcome": FetchOutcome.FETCHED}
        )
        == 1
    )
    assert logs.named("recording_url_egress_refused") == []


async def test_youtube_video_over_the_cap_is_refused_from_its_metadata_before_downloading() -> None:
    metrics = fresh_metrics()
    fetcher = build_youtube_fetcher(LIMITS, metrics)

    with captured_json_logs() as logs, pytest.raises(RecordingTooLongError):
        await fetcher.fetch(parse_youtube_video(LONG_VIDEO))

    assert logs.named("recording_url_hosts") == []
