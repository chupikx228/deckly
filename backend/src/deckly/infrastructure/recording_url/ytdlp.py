import asyncio
import importlib.util
import os
import signal
import sys
import sysconfig
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

YT_DLP_COMMAND = (sys.executable, "-I", "-m", "yt_dlp")
AUDIO_FORMAT = "bestaudio[ext=m4a][protocol=https]"
EXTRACTOR = "youtube"
PROBE_NAME = "probe"
PROBE_INFO = f"{PROBE_NAME}.info.json"
DOWNLOAD_NAME = "recording"
DOWNLOAD_EXTENSION = "m4a"
DOWNLOADED_FILE = f"{DOWNLOAD_NAME}.{DOWNLOAD_EXTENSION}"
ATTEMPTS = "1"
STDERR_TAIL_BYTES = 16 * 1024
READ_CHUNK_BYTES = 4096
MAX_INFO_BYTES = 16 * 1024 * 1024
DENO_DIRECTORY = "deno"
DENO_EXECUTABLE = "deno"
SCRIPTS_PATH = "scripts"
REQUIRED_MODULES = ("yt_dlp", "yt_dlp_ejs")
ERROR_PREFIX = "ERROR:"


def bundled_deno() -> Path:
    return Path(sysconfig.get_path(SCRIPTS_PATH)) / DENO_EXECUTABLE


def require_runtime(deno_path: Path) -> None:
    missing = [module for module in REQUIRED_MODULES if importlib.util.find_spec(module) is None]
    if missing:
        message = f"recording url ingestion needs the {', '.join(missing)} package(s); run uv sync"
        raise RuntimeError(message)
    if not (deno_path.is_file() and os.access(deno_path, os.X_OK)):
        message = f"recording url ingestion needs the deno binary at {deno_path}; run uv sync"
        raise RuntimeError(message)


@dataclass(frozen=True, slots=True)
class YtDlpInvocation:
    deno_path: Path
    socket_timeout_seconds: int
    command: tuple[str, ...] = YT_DLP_COMMAND

    def probe(self, proxy_url: str, watch_url: str) -> list[str]:
        return [
            *self._common(proxy_url),
            "--skip-download",
            "--write-info-json",
            "--output",
            f"{PROBE_NAME}.%(ext)s",
            "--",
            watch_url,
        ]

    def download(self, proxy_url: str, max_bytes: int, max_seconds: int) -> list[str]:
        return [
            *self._common(proxy_url),
            "--load-info-json",
            PROBE_INFO,
            "--downloader",
            "native",
            "--fixup",
            "never",
            "--no-mtime",
            "--max-filesize",
            str(max_bytes),
            "--match-filters",
            f"duration <= {max_seconds} & !is_live",
            "--output",
            f"{DOWNLOAD_NAME}.%(ext)s",
        ]

    def _common(self, proxy_url: str) -> list[str]:
        return [
            *self.command,
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-cache-dir",
            "--no-cookies",
            "--no-remote-components",
            "--no-js-runtimes",
            "--js-runtimes",
            f"deno:{self.deno_path}",
            "--use-extractors",
            EXTRACTOR,
            "--no-playlist",
            "--proxy",
            proxy_url,
            "--socket-timeout",
            str(self.socket_timeout_seconds),
            "--retries",
            ATTEMPTS,
            "--extractor-retries",
            ATTEMPTS,
            "--quiet",
            "--no-progress",
            "--color",
            "never",
            "--format",
            AUDIO_FORMAT,
        ]


@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stderr: str

    @property
    def error(self) -> str:
        errors = [line for line in self.stderr.splitlines() if line.startswith(ERROR_PREFIX)]
        return errors[-1] if errors else ""


def isolated_environment(workdir: Path) -> dict[str, str]:
    return {
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "DENO_DIR": str(workdir / DENO_DIRECTORY),
        "DENO_NO_UPDATE_CHECK": "1",
        "PATH": "",
        "LC_ALL": "C.UTF-8",
    }


def kill_group(process_id: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process_id, signal.SIGKILL)


async def stderr_tail(stream: asyncio.StreamReader | None) -> str:
    kept = bytearray()
    if stream is None:
        return ""
    while chunk := await stream.read(READ_CHUNK_BYTES):
        kept += chunk
        del kept[:-STDERR_TAIL_BYTES]
    return kept.decode(errors="replace")


async def run_isolated(arguments: Sequence[str], workdir: Path) -> Completed:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=workdir,
        env=isolated_environment(workdir),
        stdin=DEVNULL,
        stdout=DEVNULL,
        stderr=PIPE,
        start_new_session=True,
    )
    try:
        stderr = await stderr_tail(process.stderr)
        returncode = await process.wait()
    except BaseException:
        kill_group(process.pid)
        await process.wait()
        raise
    return Completed(returncode=returncode, stderr=stderr)


class VideoInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    title: str | None = None
    duration: float | None = Field(default=None, allow_inf_nan=False)
    live_status: str | None = None
    ext: str | None = None
    protocol: str | None = None
    filesize: int | None = None
    filesize_approx: int | None = None

    @property
    def expected_bytes(self) -> int | None:
        return self.filesize if self.filesize is not None else self.filesize_approx


class UnreadableInfoError(Exception):
    pass


def read_info(path: Path) -> VideoInfo:
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_INFO_BYTES + 1)
    except OSError as error:
        message = "yt-dlp wrote no metadata"
        raise UnreadableInfoError(message) from error
    if len(raw) > MAX_INFO_BYTES:
        message = f"yt-dlp wrote more than {MAX_INFO_BYTES} bytes of metadata"
        raise UnreadableInfoError(message)
    try:
        return VideoInfo.model_validate_json(raw)
    except ValidationError as error:
        message = "yt-dlp wrote metadata in an unexpected shape"
        raise UnreadableInfoError(message) from error
