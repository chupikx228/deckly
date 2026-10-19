import asyncio
import os
import time
from pathlib import Path

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.exceptions import (
    DocumentTooLargeError,
    RecordingTooLongError,
    RecordingUnavailableError,
)
from deckly.infrastructure.observability.metrics import Metrics
from deckly.infrastructure.recording_url.egress import EgressRefusal, EgressSession
from deckly.infrastructure.recording_url.fetcher import (
    FETCH_RETRY_AFTER_SECONDS,
    EgressRefusedError,
    FetchOutcome,
    RecordingBlockedError,
    check_run,
    egress_limits,
    filename_for,
)
from deckly.infrastructure.recording_url.ytdlp import Completed, YtDlpInvocation
from tests.fake_ytdlp import (
    FAKE_DENO,
    TEST_MAX_BYTES,
    TEST_MAX_SECONDS,
    VIDEO,
    FakeYtDlp,
    fake_fetcher,
    video_info,
)
from tests.fakes import fresh_metrics
from tests.recording_files import FIXTURES

pytestmark = pytest.mark.anyio

RECORDING = FIXTURES / "tone-fragmented.m4a"
RECORDING_BYTES = RECORDING.read_bytes()
HARDENING_FLAGS = (
    "--ignore-config",
    "--no-plugin-dirs",
    "--no-cache-dir",
    "--no-cookies",
    "--no-remote-components",
    "--no-js-runtimes",
    "--no-playlist",
)


def write_bytes(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def good_download() -> dict[str, object]:
    return {"file": str(RECORDING)}


def outcome_count(metrics: Metrics, outcome: FetchOutcome) -> float | None:
    return metrics.registry.get_sample_value("deckly_recording_url_fetches_total", {"outcome": outcome})


def is_gone(directory: str) -> bool:
    return not Path(directory).exists()


def same_directory(left: str, right: str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_gone(process_id: int, timeout_seconds: float = 5) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while process_exists(process_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not process_exists(process_id)


def wait_for_pid(pid_file: Path, timeout_seconds: float = 10) -> int:
    deadline = time.monotonic() + timeout_seconds
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return int(pid_file.read_text())


async def test_supported_video_is_probed_then_downloaded_and_returned_with_a_title_filename(
    tmp_path: Path,
) -> None:
    fake = FakeYtDlp(tmp_path, {"info": video_info()}, good_download())
    metrics = fresh_metrics()

    upload = await fake_fetcher(fake, metrics=metrics).fetch(VIDEO)

    assert upload.content == RECORDING_BYTES
    assert upload.filename == "Lecture 1: Introduction.m4a"
    assert [len(invocation.args) > 0 for invocation in fake.invocations] == [True, True]
    assert outcome_count(metrics, FetchOutcome.FETCHED) == 1


async def test_only_the_canonical_url_built_from_the_video_id_reaches_the_command_line(
    tmp_path: Path,
) -> None:
    fake = FakeYtDlp(tmp_path, {"info": video_info()}, good_download())

    await fake_fetcher(fake).fetch(VIDEO)

    probe, download = fake.invocations
    assert probe.args[-2:] == ["--", "https://www.youtube.com/watch?v=jNQXAC9IVRw"]
    assert "--skip-download" in probe.args
    assert "--write-info-json" in probe.args
    assert download.value_after("--load-info-json") == "probe.info.json"
    assert "https://www.youtube.com/watch?v=jNQXAC9IVRw" not in download.args
    for invocation in (probe, download):
        assert all(flag in invocation.args for flag in HARDENING_FLAGS)
        assert invocation.value_after("--use-extractors") == "youtube"
        assert invocation.value_after("--js-runtimes") == f"deno:{FAKE_DENO}"
        assert invocation.value_after("--format") == "bestaudio[ext=m4a][protocol=https]"
        assert invocation.value_after("--proxy").startswith("http://deckly:")
        assert invocation.value_after("--proxy").endswith(tuple(f":{port}" for port in range(1, 65536)))
    assert download.value_after("--downloader") == "native"
    assert download.value_after("--fixup") == "never"
    assert download.value_after("--max-filesize") == str(TEST_MAX_BYTES)
    assert download.value_after("--match-filters") == f"duration <= {TEST_MAX_SECONDS} & !is_live"


async def test_downloader_runs_with_an_empty_path_in_a_private_directory_that_is_removed_afterwards(
    tmp_path: Path,
) -> None:
    fake = FakeYtDlp(tmp_path, {"info": video_info()}, good_download())

    await fake_fetcher(fake).fetch(VIDEO)

    probe, download = fake.invocations
    assert probe.cwd == download.cwd
    assert is_gone(probe.cwd)
    assert set(probe.env) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == {
        "HOME",
        "TMPDIR",
        "DENO_DIR",
        "DENO_NO_UPDATE_CHECK",
        "PATH",
        "LC_ALL",
    }
    assert probe.env["PATH"] == ""
    assert same_directory(probe.env["HOME"], probe.cwd)


@pytest.mark.parametrize(
    ("info", "error"),
    [
        (video_info(duration=TEST_MAX_SECONDS + 0.5), RecordingTooLongError),
        (video_info(duration=10 * 60 * 60), RecordingTooLongError),
        (video_info(live_status="is_live", duration=None), RecordingUnavailableError),
        (video_info(live_status="is_upcoming"), RecordingUnavailableError),
        (video_info(live_status="post_live"), RecordingUnavailableError),
        (video_info(live_status=None), RecordingUnavailableError),
        (video_info(duration=None), RecordingUnavailableError),
        (video_info(duration=0), RecordingUnavailableError),
        (video_info(ext="webm"), RecordingUnavailableError),
        (video_info(protocol="m3u8_native"), RecordingUnavailableError),
        (video_info(filesize=TEST_MAX_BYTES + 1), DocumentTooLargeError),
        (video_info(filesize=None, filesize_approx=TEST_MAX_BYTES + 1), DocumentTooLargeError),
        (video_info(id="aaaaaaaaaaa"), UpstreamUnavailableError),
        ("{not json", UpstreamUnavailableError),
        ('{"title": "no id"}', UpstreamUnavailableError),
        (video_info(duration="NaN"), UpstreamUnavailableError),
    ],
    ids=[
        "just-over-the-cap",
        "ten-hours",
        "live",
        "upcoming",
        "post-live",
        "no-live-status",
        "no-duration",
        "zero-duration",
        "not-m4a",
        "not-https",
        "filesize-over-limit",
        "approximate-size-over-limit",
        "different-video",
        "malformed-json",
        "missing-id",
        "nan-duration",
    ],
)
async def test_metadata_refusal_happens_before_any_audio_is_downloaded(
    tmp_path: Path, info: object, error: type[Exception]
) -> None:
    fake = FakeYtDlp(tmp_path, {"info": info}, good_download())

    with pytest.raises(error):
        await fake_fetcher(fake).fetch(VIDEO)

    assert len(fake.invocations) == 1


async def test_metadata_exactly_at_the_cap_and_size_limit_is_downloaded(tmp_path: Path) -> None:
    fake = FakeYtDlp(
        tmp_path, {"info": video_info(duration=TEST_MAX_SECONDS, filesize=TEST_MAX_BYTES)}, good_download()
    )

    await fake_fetcher(fake).fetch(VIDEO)

    assert len(fake.invocations) == 2


@pytest.mark.parametrize(
    ("stderr", "error", "outcome"),
    [
        (
            "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you\N{RIGHT SINGLE QUOTATION MARK}re not a bot",
            RecordingBlockedError,
            FetchOutcome.BLOCKED,
        ),
        (
            "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot",
            RecordingBlockedError,
            FetchOutcome.BLOCKED,
        ),
        (
            "ERROR: unable to download: HTTP Error 429: Too Many Requests",
            RecordingBlockedError,
            FetchOutcome.BLOCKED,
        ),
        (
            "ERROR: [youtube] jNQXAC9IVRw: Video unavailable",
            RecordingUnavailableError,
            FetchOutcome.UNAVAILABLE,
        ),
        ("ERROR: [youtube] jNQXAC9IVRw: Private video", RecordingUnavailableError, FetchOutcome.UNAVAILABLE),
        (
            "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm your age",
            RecordingUnavailableError,
            FetchOutcome.UNAVAILABLE,
        ),
        (
            "ERROR: [youtube] jNQXAC9IVRw: This live event will begin in 3 hours.",
            RecordingUnavailableError,
            FetchOutcome.UNAVAILABLE,
        ),
        ("ERROR: Requested format is not available", RecordingUnavailableError, FetchOutcome.UNAVAILABLE),
        ("ERROR: something nobody has seen before", UpstreamUnavailableError, FetchOutcome.FAILED),
        ("", UpstreamUnavailableError, FetchOutcome.FAILED),
    ],
)
async def test_yt_dlp_failures_are_classified_by_their_error_line(
    tmp_path: Path, stderr: str, error: type[Exception], outcome: FetchOutcome
) -> None:
    fake = FakeYtDlp(tmp_path, {"stderr": f"WARNING: noise\n{stderr}\n", "exit": 1}, good_download())
    metrics = fresh_metrics()

    with pytest.raises(error) as raised:
        await fake_fetcher(fake, metrics=metrics).fetch(VIDEO)

    assert type(raised.value) is error
    assert outcome_count(metrics, outcome) == 1
    if isinstance(raised.value, UpstreamUnavailableError):
        assert raised.value.retry_after_seconds == FETCH_RETRY_AFTER_SECONDS


RATE_LIMITED_STDERR = (
    "WARNING: [youtube] aISXCw0Pi94: Unable to download webpage: HTTP Error 429: Too Many Requests "
    "(caused by <HTTPError 429: Too Many Requests>)\n"
    "WARNING: [youtube] Unable to fetch GVS PO Token for web client: Missing required Visitor Data. "
    'You may need to pass Visitor Data with --extractor-args "youtube:visitor_data=XXX"\n'
    "ERROR: [youtube] aISXCw0Pi94: This video is not available\n"
)
UNRELATED_WARNINGS_STDERR = (
    "WARNING: [youtube] x429Abc12345: Unable to fetch GVS PO Token for web client: Missing required "
    "Visitor Data\n"
    "ERROR: [youtube] x429Abc12345: This video is not available\n"
)
PRIVATE_VIDEO_STDERR = (
    "WARNING: [youtube] aISXCw0Pi94: Some other warning\nERROR: [youtube] aISXCw0Pi94: Private video\n"
)


@pytest.mark.parametrize(
    ("stderr", "error", "outcome"),
    [
        (RATE_LIMITED_STDERR, RecordingBlockedError, FetchOutcome.BLOCKED),
        (UNRELATED_WARNINGS_STDERR, RecordingUnavailableError, FetchOutcome.UNAVAILABLE),
        (PRIVATE_VIDEO_STDERR, RecordingUnavailableError, FetchOutcome.UNAVAILABLE),
    ],
)
async def test_rate_limit_warning_before_a_generic_error_is_blocked_not_unavailable(
    tmp_path: Path, stderr: str, error: type[Exception], outcome: FetchOutcome
) -> None:
    fake = FakeYtDlp(tmp_path, {"stderr": stderr, "exit": 1}, good_download())
    metrics = fresh_metrics()

    with pytest.raises(error) as raised:
        await fake_fetcher(fake, metrics=metrics).fetch(VIDEO)

    assert type(raised.value) is error
    assert outcome_count(metrics, outcome) == 1
    other = FetchOutcome.UNAVAILABLE if outcome is FetchOutcome.BLOCKED else FetchOutcome.BLOCKED
    assert outcome_count(metrics, other) is None


async def test_rate_limit_during_the_download_is_blocked_too(tmp_path: Path) -> None:
    fake = FakeYtDlp(tmp_path, {"info": video_info()}, {"stderr": RATE_LIMITED_STDERR, "exit": 1})
    metrics = fresh_metrics()

    with pytest.raises(RecordingBlockedError):
        await fake_fetcher(fake, metrics=metrics).fetch(VIDEO)

    assert outcome_count(metrics, FetchOutcome.BLOCKED) == 1
    assert outcome_count(metrics, FetchOutcome.UNAVAILABLE) is None


async def test_download_failure_after_a_good_probe_is_classified_too(tmp_path: Path) -> None:
    fake = FakeYtDlp(
        tmp_path,
        {"info": video_info()},
        {"stderr": "ERROR: unable to download video data: HTTP Error 403: Forbidden\n", "exit": 1},
    )

    with pytest.raises(UpstreamUnavailableError):
        await fake_fetcher(fake).fetch(VIDEO)


async def test_download_that_writes_no_file_is_an_upstream_failure(tmp_path: Path) -> None:
    fake = FakeYtDlp(tmp_path, {"info": video_info()}, {"exit": 0})

    with pytest.raises(UpstreamUnavailableError):
        await fake_fetcher(fake).fetch(VIDEO)


async def test_downloaded_file_over_the_size_limit_is_refused(tmp_path: Path) -> None:
    oversized = write_bytes(tmp_path / "oversized.m4a", b"\x00" * (TEST_MAX_BYTES + 1))
    fake = FakeYtDlp(tmp_path, {"info": video_info(filesize=None)}, {"file": str(oversized)})

    with pytest.raises(DocumentTooLargeError):
        await fake_fetcher(fake).fetch(VIDEO)


async def test_attempt_to_tunnel_anywhere_off_the_allowlist_fails_the_fetch(tmp_path: Path) -> None:
    fake = FakeYtDlp(tmp_path, {"connect": "169.254.169.254:443", "info": video_info()}, good_download())
    metrics = fresh_metrics()

    with pytest.raises(EgressRefusedError):
        await fake_fetcher(fake, metrics=metrics).fetch(VIDEO)

    assert len(fake.invocations) == 1
    assert outcome_count(metrics, FetchOutcome.EGRESS_REFUSED) == 1
    assert (
        metrics.registry.get_sample_value(
            "deckly_recording_url_egress_refusals_total", {"reason": EgressRefusal.HOST}
        )
        == 1
    )


async def test_fetch_over_its_deadline_kills_yt_dlp_and_everything_it_started(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    fake = FakeYtDlp(tmp_path, {"child_pid_file": str(pid_file), "sleep": 60}, good_download())
    metrics = fresh_metrics()
    started = time.monotonic()

    with pytest.raises(UpstreamUnavailableError):
        await fake_fetcher(fake, metrics=metrics, timeout_seconds=1.5).fetch(VIDEO)

    assert time.monotonic() - started < 10
    assert outcome_count(metrics, FetchOutcome.TIMED_OUT) == 1
    assert await asyncio.to_thread(wait_until_gone, wait_for_pid(pid_file))
    assert is_gone(fake.invocations[0].cwd)


async def test_cancelled_fetch_kills_yt_dlp_too(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    fake = FakeYtDlp(tmp_path, {"child_pid_file": str(pid_file), "sleep": 60}, good_download())
    task = asyncio.ensure_future(fake_fetcher(fake).fetch(VIDEO))
    child = await asyncio.to_thread(wait_for_pid, pid_file)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await asyncio.to_thread(wait_until_gone, child)


def session_with(*refusals: EgressRefusal) -> EgressSession:
    session = EgressSession("token", egress_limits(TEST_MAX_BYTES))
    session.refusals.extend(refusals)
    return session


def test_byte_budget_refusal_means_the_recording_is_too_large() -> None:
    with pytest.raises(DocumentTooLargeError):
        check_run(Completed(returncode=1, stderr=""), session_with(EgressRefusal.BYTES), TEST_MAX_BYTES)


def test_any_other_refusal_wins_over_what_yt_dlp_printed() -> None:
    completed = Completed(returncode=1, stderr="ERROR: Video unavailable")

    with pytest.raises(EgressRefusedError):
        check_run(completed, session_with(EgressRefusal.ADDRESS), TEST_MAX_BYTES)


def test_successful_run_without_refusals_passes() -> None:
    check_run(Completed(returncode=0, stderr="WARNING: harmless"), session_with(), TEST_MAX_BYTES)


def test_last_error_line_is_the_one_classified() -> None:
    completed = Completed(returncode=1, stderr="ERROR: first\nWARNING: x\nERROR: second\n")

    assert completed.error == "ERROR: second"


def test_warning_lines_are_collected_apart_from_errors() -> None:
    completed = Completed(returncode=1, stderr="WARNING: a\nERROR: b\nnoise WARNING: c\nWARNING: d\n")

    assert completed.warnings == ["WARNING: a", "WARNING: d"]


@pytest.mark.parametrize(
    ("title", "filename"),
    [
        ("Lecture 1: Introduction", "Lecture 1: Introduction.m4a"),
        ("AC/DC \\ live", "AC-DC - live.m4a"),
        (None, "jNQXAC9IVRw.m4a"),
        ("", "jNQXAC9IVRw.m4a"),
        ("\N{ZERO WIDTH SPACE}\N{RIGHT-TO-LEFT OVERRIDE}\N{NULL}", "jNQXAC9IVRw.m4a"),
        ("  padded  ", "padded.m4a"),
        ("x" * 500, "x" * 200 + ".m4a"),
        ("evil\N{RIGHT-TO-LEFT OVERRIDE}gpj.exe", "evilgpj.exe.m4a"),
    ],
)
def test_filename_comes_from_the_cleaned_title_or_falls_back_to_the_video_id(
    title: str | None, filename: str
) -> None:
    assert filename_for(title, VIDEO) == filename


def test_production_invocation_runs_the_installed_yt_dlp_in_isolated_mode() -> None:
    invocation = YtDlpInvocation(deno_path=FAKE_DENO, socket_timeout_seconds=15)

    assert invocation.command[1:] == ("-I", "-m", "yt_dlp")
