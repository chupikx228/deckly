import asyncio
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from deckly.infrastructure.recording_url.ytdlp import kill_group, stderr_tail

DRAIN_CHUNK_BYTES = 64 * 1024

type Stop = Callable[[], None]
type Consumer[T] = Callable[[asyncio.StreamReader, Stop], Awaitable[T]]


@dataclass(frozen=True, slots=True)
class Finished[T]:
    output: T
    returncode: int
    stderr: str

    @property
    def last_error_line(self) -> str:
        lines = [line for line in self.stderr.splitlines() if line.strip()]
        return lines[-1] if lines else ""


async def drain(source: asyncio.StreamReader) -> None:
    while await source.read(DRAIN_CHUNK_BYTES):
        pass


def stream(pipe: asyncio.StreamReader | None) -> asyncio.StreamReader:
    if pipe is None:
        message = "the subprocess was started without a pipe"
        raise RuntimeError(message)
    return pipe


async def supervise[T](
    arguments: Sequence[str], *, environment: Mapping[str, str], workdir: Path, consume: Consumer[T]
) -> Finished[T]:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=workdir,
        env=dict(environment),
        stdin=DEVNULL,
        stdout=PIPE,
        stderr=PIPE,
        start_new_session=True,
    )
    stdout = stream(process.stdout)
    errors = asyncio.create_task(stderr_tail(stream(process.stderr)))
    try:
        output = await consume(stdout, lambda: kill_group(process.pid))
        await drain(stdout)
        returncode = await process.wait()
    except BaseException:
        kill_group(process.pid)
        await drain(stdout)
        await process.wait()
        await errors
        raise
    return Finished(output=output, returncode=returncode, stderr=await errors)
