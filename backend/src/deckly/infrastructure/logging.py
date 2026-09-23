import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final

RESERVED_RECORD_ATTRIBUTES: Final = frozenset(
    logging.LogRecord("", logging.NOTSET, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName", "color_message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            key: value for key, value in record.__dict__.items() if key not in RESERVED_RECORD_ATTRIBUTES
        }
        payload.update(
            timestamp=datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq"):
        library_logger = logging.getLogger(name)
        library_logger.handlers = []
        library_logger.propagate = True
