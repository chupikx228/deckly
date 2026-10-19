import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

FAKE_PROGRAM = """#!{python}
import json, os, subprocess, sys, time

SCENARIO = json.loads({scenario!r})
if SCENARIO.get("record"):
    with open(SCENARIO["record"], "a") as record:
        entry = {{"args": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}}
        record.write(json.dumps(entry) + "\\n")
if SCENARIO.get("child_pid_file"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(SCENARIO["child_pid_file"], "w") as pid_file:
        pid_file.write(str(child.pid))
sys.stderr.write(SCENARIO.get("stderr", ""))
sys.stderr.flush()
for step in SCENARIO.get("steps", []):
    time.sleep(step["delay"])
    sys.stdout.write(step["text"])
    sys.stdout.flush()
if SCENARIO.get("stdout_text"):
    sys.stdout.write(SCENARIO["stdout_text"])
    sys.stdout.flush()
remaining = SCENARIO.get("stdout_bytes", 0)
while remaining > 0:
    chunk = min(remaining, 65536)
    sys.stdout.buffer.write(bytes([1, 0]) * (chunk // 2) + b"\\x01" * (chunk % 2))
    remaining -= chunk
sys.stdout.buffer.flush()
if SCENARIO.get("stdout_forever"):
    while True:
        sys.stdout.buffer.write(b"\\x00" * 65536)
        sys.stdout.buffer.flush()
if SCENARIO.get("sleep"):
    time.sleep(SCENARIO["sleep"])
sys.exit(SCENARIO.get("exit", 0))
"""


@dataclass(frozen=True, slots=True)
class Invocation:
    args: list[str]
    cwd: str
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class FakeProgram:
    path: Path
    record: Path

    @property
    def invocations(self) -> list[Invocation]:
        if not self.record.exists():
            return []
        return [Invocation(**json.loads(line)) for line in self.record.read_text().splitlines()]


def fake_program(directory: Path, name: str, scenario: dict[str, object]) -> FakeProgram:
    record = directory / f"{name}.invocations.jsonl"
    path = directory / name
    path.write_text(
        FAKE_PROGRAM.format(python=sys.executable, scenario=json.dumps({**scenario, "record": str(record)}))
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return FakeProgram(path=path, record=record)
