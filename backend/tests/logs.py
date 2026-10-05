import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from deckly.infrastructure.logging import (
    URL_LOGGING_LIBRARY_LOGGERS,
    json_handler,
    quiet_url_logging_libraries,
)

type LogLine = dict[str, object]


class JsonLogs:
    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def text(self) -> str:
        return self._stream.getvalue()

    def lines(self) -> list[LogLine]:
        parsed: list[LogLine] = []
        for raw in self.text.splitlines():
            line: object = json.loads(raw)
            assert isinstance(line, dict)
            parsed.append({str(key): value for key, value in line.items()})
        return parsed

    def named(self, message: str) -> list[LogLine]:
        return [line for line in self.lines() if line["message"] == message]

    def for_field(self, field: str, value: str) -> list[LogLine]:
        return [line for line in self.lines() if line.get(field) == value]


@contextmanager
def captured_json_logs(level: int = logging.DEBUG) -> Iterator[JsonLogs]:
    stream = io.StringIO()
    handler = json_handler(stream)
    root = logging.getLogger()
    saved_level = root.level
    saved_library_levels = {name: logging.getLogger(name).level for name in URL_LOGGING_LIBRARY_LOGGERS}
    root.addHandler(handler)
    root.setLevel(level)
    quiet_url_logging_libraries()
    try:
        yield JsonLogs(stream)
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_level)
        for name, library_level in saved_library_levels.items():
            logging.getLogger(name).setLevel(library_level)
