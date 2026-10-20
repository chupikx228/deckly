import asyncio
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from deckly.domain.exceptions import ArticleUnavailableError
from deckly.infrastructure.article_url.extracted import ExtractedArticle
from deckly.infrastructure.transcription.process import Stop, supervise

EXTRACTOR_MODULE = "deckly.infrastructure.article_url.extractor"
EXTRACTOR_COMMAND = (sys.executable, "-I", "-m", EXTRACTOR_MODULE)
WORKDIR_PREFIX = "deckly-article-"
PAGE_FILE = "page.html"
READ_CHUNK_BYTES = 64 * 1024
MAX_JSON_BYTES_PER_CHARACTER = 6
OUTPUT_OVERHEAD_BYTES = 64 * 1024
CHARSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,39}")
REQUIRED_MODULES = ("trafilatura",)


def unreadable(detail: str) -> ArticleUnavailableError:
    error = ArticleUnavailableError("the page could not be read")
    error.add_note(detail)
    return error


def require_extractor() -> None:
    missing = [module for module in REQUIRED_MODULES if importlib.util.find_spec(module) is None]
    if missing:
        message = f"article url ingestion needs the {', '.join(missing)} package(s); run uv sync"
        raise RuntimeError(message)


def safe_charset(charset: str | None) -> str | None:
    if charset is None or CHARSET.fullmatch(charset) is None:
        return None
    return charset


@dataclass(frozen=True, slots=True)
class ExtractorInvocation:
    max_characters: int
    command: tuple[str, ...] = EXTRACTOR_COMMAND

    @property
    def max_output_bytes(self) -> int:
        return self.max_characters * MAX_JSON_BYTES_PER_CHARACTER + OUTPUT_OVERHEAD_BYTES

    def arguments(self, page: Path, charset: str | None) -> list[str]:
        checked = safe_charset(charset)
        charset_arguments = [] if checked is None else [f"--charset={checked}"]
        return [
            *self.command,
            f"--max-characters={self.max_characters}",
            *charset_arguments,
            "--",
            str(page),
        ]

    @staticmethod
    def environment(workdir: Path) -> dict[str, str]:
        return {
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "PATH": "",
            "LC_ALL": "C.UTF-8",
        }


async def read_capped(source: asyncio.StreamReader, stop: Stop, limit: int) -> bytes | None:
    output = bytearray()
    while chunk := await source.read(READ_CHUNK_BYTES):
        output += chunk
        if len(output) > limit:
            stop()
            return None
    return bytes(output)


@dataclass(frozen=True, slots=True)
class SubprocessArticleExtractor:
    invocation: ExtractorInvocation
    deadline_seconds: float

    async def extract(self, content: bytes, charset: str | None) -> ExtractedArticle:
        limit = self.invocation.max_output_bytes
        with TemporaryDirectory(prefix=WORKDIR_PREFIX) as directory:
            workdir = Path(directory)
            page = workdir / PAGE_FILE
            await asyncio.to_thread(page.write_bytes, content)
            try:
                async with asyncio.timeout(self.deadline_seconds):
                    finished = await supervise(
                        self.invocation.arguments(page, charset),
                        environment=self.invocation.environment(workdir),
                        workdir=workdir,
                        consume=lambda source, stop: read_capped(source, stop, limit),
                    )
            except TimeoutError as error:
                message = f"the article extractor did not finish within {self.deadline_seconds} seconds"
                raise unreadable(message) from error
        if finished.output is None:
            message = f"the article extractor wrote more than {limit} bytes"
            raise unreadable(message)
        if finished.returncode != 0:
            message = f"the article extractor exited {finished.returncode}: {finished.last_error_line}"
            raise unreadable(message)
        try:
            return ExtractedArticle.model_validate_json(finished.output)
        except ValidationError as error:
            message = "the article extractor answered in an unexpected shape"
            raise unreadable(message) from error
