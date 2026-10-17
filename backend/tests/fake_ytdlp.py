import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from deckly.domain.recording_url import YouTubeVideo
from deckly.infrastructure.observability.metrics import Metrics
from deckly.infrastructure.recording_url.egress import EgressProxy
from deckly.infrastructure.recording_url.fetcher import (
    YOUTUBE_EGRESS_POLICY,
    FetchLimits,
    YtDlpRecordingFetcher,
    egress_limits,
)
from deckly.infrastructure.recording_url.ytdlp import YtDlpInvocation
from tests.fakes import fresh_metrics

VIDEO = YouTubeVideo("jNQXAC9IVRw")
TEST_MAX_BYTES = 64 * 1024
TEST_MAX_SECONDS = 60
FAKE_DENO = Path("/nonexistent/deno")
FAKE_SCRIPT = """
import base64, json, os, shutil, socket, subprocess, sys, time
from urllib.parse import urlsplit

SCENARIO = json.loads({scenario!r})
args = sys.argv[1:]
with open(SCENARIO["record"], "a") as record:
    record.write(json.dumps({{"args": args, "cwd": os.getcwd(), "env": dict(os.environ)}}) + "\\n")
step = SCENARIO["probe" if "--skip-download" in args else "download"]
if step.get("connect"):
    proxy = urlsplit(args[args.index("--proxy") + 1])
    credentials = base64.b64encode(f"{{proxy.username}}:{{proxy.password}}".encode()).decode()
    head = f"CONNECT {{step['connect']}} HTTP/1.1\\r\\nProxy-Authorization: Basic {{credentials}}"
    with socket.create_connection((proxy.hostname, proxy.port)) as tunnel:
        tunnel.sendall((head + "\\r\\n\\r\\n").encode())
        tunnel.recv(1024)
if step.get("child_pid_file"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(step["child_pid_file"], "w") as pid_file:
        pid_file.write(str(child.pid))
if step.get("sleep"):
    time.sleep(step["sleep"])
if "info" in step:
    with open("probe.info.json", "w") as info:
        info.write(step["info"] if isinstance(step["info"], str) else json.dumps(step["info"]))
if "file" in step:
    shutil.copyfile(step["file"], "recording.m4a")
sys.stderr.write(step.get("stderr", ""))
sys.exit(step.get("exit", 0))
"""


def video_info(**overrides: object) -> dict[str, object]:
    return {
        "id": VIDEO.video_id,
        "title": "Lecture 1: Introduction",
        "duration": 2.0,
        "live_status": "not_live",
        "ext": "m4a",
        "protocol": "https",
        "filesize": 8013,
        **overrides,
    }


@dataclass(frozen=True, slots=True)
class Invocation:
    args: list[str]
    cwd: str
    env: dict[str, str]

    def value_after(self, flag: str) -> str:
        return self.args[self.args.index(flag) + 1]


class FakeYtDlp:
    def __init__(self, workspace: Path, probe: Mapping[str, object], download: Mapping[str, object]) -> None:
        self.record = workspace / "invocations.jsonl"
        self.script = workspace / "fake_yt_dlp.py"
        scenario = json.dumps({"record": str(self.record), "probe": probe, "download": download})
        self.script.write_text(FAKE_SCRIPT.format(scenario=scenario))

    @property
    def command(self) -> tuple[str, ...]:
        return (sys.executable, "-I", str(self.script))

    @property
    def invocations(self) -> list[Invocation]:
        if not self.record.exists():
            return []
        return [Invocation(**json.loads(line)) for line in self.record.read_text().splitlines()]


async def no_resolution(host: str, port: int) -> list[str]:
    raise OSError(host, port)


def fake_fetcher(
    fake: FakeYtDlp,
    *,
    metrics: Metrics | None = None,
    timeout_seconds: float = 30,
    max_bytes: int = TEST_MAX_BYTES,
) -> YtDlpRecordingFetcher:
    observed = metrics or fresh_metrics()
    return YtDlpRecordingFetcher(
        invocation=YtDlpInvocation(deno_path=FAKE_DENO, socket_timeout_seconds=15, command=fake.command),
        proxy=EgressProxy(YOUTUBE_EGRESS_POLICY, egress_limits(max_bytes), observed, resolver=no_resolution),
        limits=FetchLimits(
            max_bytes=max_bytes, max_duration_seconds=TEST_MAX_SECONDS, timeout_seconds=timeout_seconds
        ),
        metrics=observed,
    )
