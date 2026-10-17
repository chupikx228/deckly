import asyncio
import logging
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import DocumentUpload
from deckly.domain.document import clean_filename
from deckly.domain.exceptions import (
    DocumentTooLargeError,
    InvalidDocumentError,
    RecordingTooLongError,
    RecordingUnavailableError,
)
from deckly.domain.recording_url import YouTubeVideo
from deckly.infrastructure.observability.metrics import Metrics
from deckly.infrastructure.recording_url.egress import (
    EgressLimits,
    EgressPolicy,
    EgressProxy,
    EgressRefusal,
    EgressSession,
)
from deckly.infrastructure.recording_url.ytdlp import (
    DOWNLOAD_EXTENSION,
    DOWNLOADED_FILE,
    PROBE_INFO,
    Completed,
    UnreadableInfoError,
    VideoInfo,
    YtDlpInvocation,
    bundled_deno,
    read_info,
    require_runtime,
    run_isolated,
)

logger = logging.getLogger(__name__)

FETCH_RETRY_AFTER_SECONDS = 60
EXTRACTION_ALLOWANCE_BYTES = 16 * 1024 * 1024
MAX_TUNNELS_PER_FETCH = 32
HEAD_TIMEOUT_SECONDS = 10.0
RESOLVE_TIMEOUT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 10.0
SOCKET_TIMEOUT_SECONDS = 15
YOUTUBE_EGRESS_POLICY = EgressPolicy(
    hosts=frozenset({"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com"}),
    host_suffixes=(".googlevideo.com",),
)
WORKDIR_PREFIX = "deckly-recording-"
PLAYABLE_LIVE_STATUSES = frozenset({"not_live", "was_live"})
EXPECTED_PROTOCOL = "https"
MAX_TITLE_CHARACTERS = 200
PATH_SEPARATORS = ("/", "\\")
TITLE_SEPARATOR_REPLACEMENT = "-"
MAX_LOGGED_ERROR_CHARACTERS = 300
BLOCKED_MARKERS = (
    "not a bot",
    "http error 429",
    "too many requests",
)
UNAVAILABLE_MARKERS = (
    "video unavailable",
    "this video is unavailable",
    "this video is not available",
    "private video",
    "has been removed",
    "members-only",
    "join this channel",
    "confirm your age",
    "age-restricted",
    "inappropriate for some users",
    "not available in your country",
    "geo restricted",
    "live event will begin",
    "premieres in",
    "requested format is not available",
    "account associated with this video has been terminated",
    "copyright",
)


class FetchOutcome(StrEnum):
    FETCHED = "fetched"
    TOO_LONG = "too_long"
    TOO_LARGE = "too_large"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    EGRESS_REFUSED = "egress_refused"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class RecordingBlockedError(UpstreamUnavailableError):
    pass


class EgressRefusedError(UpstreamUnavailableError):
    pass


OUTCOMES: Mapping[type[Exception], FetchOutcome] = {
    RecordingTooLongError: FetchOutcome.TOO_LONG,
    DocumentTooLargeError: FetchOutcome.TOO_LARGE,
    RecordingUnavailableError: FetchOutcome.UNAVAILABLE,
    RecordingBlockedError: FetchOutcome.BLOCKED,
    EgressRefusedError: FetchOutcome.EGRESS_REFUSED,
}


def outcome_of(error: Exception) -> FetchOutcome:
    for kind in type(error).__mro__:
        outcome = OUTCOMES.get(kind)
        if outcome is not None:
            return outcome
    return FetchOutcome.FAILED


def upstream_failure(message: str) -> UpstreamUnavailableError:
    error = UpstreamUnavailableError(FETCH_RETRY_AFTER_SECONDS)
    error.add_note(message)
    return error


def classify(completed: Completed) -> Exception:
    error = completed.error.lower()
    failure: Exception
    if any(marker in error for marker in BLOCKED_MARKERS):
        failure = RecordingBlockedError(FETCH_RETRY_AFTER_SECONDS)
    elif any(marker in error for marker in UNAVAILABLE_MARKERS):
        failure = RecordingUnavailableError(
            "the video cannot be fetched: it is private, removed or restricted"
        )
    else:
        failure = UpstreamUnavailableError(FETCH_RETRY_AFTER_SECONDS)
    failure.add_note(f"yt-dlp exited with {completed.returncode}: {completed.error}")
    return failure


def check_run(completed: Completed, session: EgressSession, max_bytes: int) -> None:
    if EgressRefusal.BYTES in session.refusals:
        message = f"the recording is over the {max_bytes} byte limit"
        raise DocumentTooLargeError(message)
    if session.refusals:
        raise EgressRefusedError(FETCH_RETRY_AFTER_SECONDS)
    if completed.returncode != 0:
        raise classify(completed)


@dataclass(frozen=True, slots=True)
class FetchLimits:
    max_bytes: int
    max_duration_seconds: int
    timeout_seconds: float


def check_metadata(info: VideoInfo, video: YouTubeVideo, limits: FetchLimits) -> None:
    if info.id != video.video_id:
        message = "yt-dlp described a different video than the one requested"
        raise upstream_failure(message)
    if info.live_status not in PLAYABLE_LIVE_STATUSES:
        message = "the video is a live stream, a premiere or not yet processed"
        raise RecordingUnavailableError(message)
    if info.duration is None or info.duration <= 0:
        message = "the video has no known duration"
        raise RecordingUnavailableError(message)
    if info.duration > limits.max_duration_seconds:
        message = f"the recording is longer than the {limits.max_duration_seconds} second limit"
        raise RecordingTooLongError(message)
    if info.ext != DOWNLOAD_EXTENSION or info.protocol != EXPECTED_PROTOCOL:
        message = "the video has no audio-only M4A stream"
        raise RecordingUnavailableError(message)
    expected_bytes = info.expected_bytes
    if expected_bytes is not None and expected_bytes > limits.max_bytes:
        message = f"the recording is {expected_bytes} bytes, over the {limits.max_bytes} byte limit"
        raise DocumentTooLargeError(message)


def filename_for(title: str | None, video: YouTubeVideo) -> str:
    stem = title or ""
    for separator in PATH_SEPARATORS:
        stem = stem.replace(separator, TITLE_SEPARATOR_REPLACEMENT)
    try:
        cleaned = clean_filename(stem[:MAX_TITLE_CHARACTERS])
    except InvalidDocumentError:
        cleaned = video.video_id
    return f"{cleaned}.{DOWNLOAD_EXTENSION}"


def read_download(path: Path, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as file:
            content = file.read(max_bytes + 1)
    except OSError as error:
        message = "yt-dlp finished without writing the recording"
        raise upstream_failure(message) from error
    if len(content) > max_bytes:
        message = f"the recording is over the {max_bytes} byte limit"
        raise DocumentTooLargeError(message)
    return content


@dataclass(frozen=True, slots=True)
class YtDlpRecordingFetcher:
    invocation: YtDlpInvocation
    proxy: EgressProxy
    limits: FetchLimits
    metrics: Metrics

    async def fetch(self, video: YouTubeVideo) -> DocumentUpload:
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                upload = await self._fetch(video)
        except TimeoutError as error:
            self._record(FetchOutcome.TIMED_OUT)
            message = f"fetching the recording took longer than {self.limits.timeout_seconds} seconds"
            raise upstream_failure(message) from error
        except Exception as error:
            self._record(outcome_of(error), error)
            raise
        self._record(FetchOutcome.FETCHED)
        return upload

    async def _fetch(self, video: YouTubeVideo) -> DocumentUpload:
        workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=WORKDIR_PREFIX))
        try:
            async with self.proxy.serve() as session:
                info = await self._probe(session, video, workdir)
                content = await self._download(session, workdir)
                logger.info("recording_url_hosts", extra={"hosts": sorted(set(session.hosts))})
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)
        return DocumentUpload(filename=filename_for(info.title, video), content=content)

    async def _probe(self, session: EgressSession, video: YouTubeVideo, workdir: Path) -> VideoInfo:
        completed = await run_isolated(self.invocation.probe(session.proxy_url, video.watch_url), workdir)
        check_run(completed, session, self.limits.max_bytes)
        try:
            info = await asyncio.to_thread(read_info, workdir / PROBE_INFO)
        except UnreadableInfoError as error:
            raise upstream_failure(str(error)) from error
        check_metadata(info, video, self.limits)
        return info

    async def _download(self, session: EgressSession, workdir: Path) -> bytes:
        arguments = self.invocation.download(
            session.proxy_url, self.limits.max_bytes, self.limits.max_duration_seconds
        )
        completed = await run_isolated(arguments, workdir)
        check_run(completed, session, self.limits.max_bytes)
        return await asyncio.to_thread(read_download, workdir / DOWNLOADED_FILE, self.limits.max_bytes)

    def _record(self, outcome: FetchOutcome, error: Exception | None = None) -> None:
        self.metrics.recording_url_fetches.labels(outcome=outcome).inc()
        context = {"outcome": outcome}
        if error is None:
            logger.info("recording_url_fetched", extra=context)
            return
        detail = " ".join(getattr(error, "__notes__", ()))[:MAX_LOGGED_ERROR_CHARACTERS]
        logger.warning("recording_url_fetch_failed", extra={**context, "detail": detail or str(error)})


def egress_limits(max_bytes: int) -> EgressLimits:
    return EgressLimits(
        max_bytes=max_bytes + EXTRACTION_ALLOWANCE_BYTES,
        max_tunnels=MAX_TUNNELS_PER_FETCH,
        head_timeout_seconds=HEAD_TIMEOUT_SECONDS,
        resolve_timeout_seconds=RESOLVE_TIMEOUT_SECONDS,
        connect_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
    )


def build_youtube_fetcher(limits: FetchLimits, metrics: Metrics) -> YtDlpRecordingFetcher:
    deno = bundled_deno()
    require_runtime(deno)
    return YtDlpRecordingFetcher(
        invocation=YtDlpInvocation(deno_path=deno, socket_timeout_seconds=SOCKET_TIMEOUT_SECONDS),
        proxy=EgressProxy(YOUTUBE_EGRESS_POLICY, egress_limits(limits.max_bytes), metrics),
        limits=limits,
        metrics=metrics,
    )
