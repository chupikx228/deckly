import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final, TextIO

from opentelemetry import trace

from deckly.application.correlation import current_correlation

TRACE_ID = "trace_id"
SPAN_ID = "span_id"
CAPTURED_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "arq")
SILENCED_LIBRARY_LOGGERS = ("uvicorn.access",)
URL_LOGGING_LIBRARY_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")

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


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        fields = dict(current_correlation())
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            fields[TRACE_ID] = trace.format_trace_id(span.trace_id)
            fields[SPAN_ID] = trace.format_span_id(span.span_id)
        for name, value in fields.items():
            if name not in record.__dict__:
                setattr(record, name, value)
        return True


def json_handler(stream: TextIO) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    return handler


def quiet_url_logging_libraries() -> None:
    for name in URL_LOGGING_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.handlers = [json_handler(sys.stdout)]
    root.setLevel(level)
    for name in CAPTURED_LIBRARY_LOGGERS:
        library_logger = logging.getLogger(name)
        library_logger.handlers = []
        library_logger.propagate = True
    for name in SILENCED_LIBRARY_LOGGERS:
        library_logger = logging.getLogger(name)
        library_logger.handlers = []
        library_logger.propagate = False
    quiet_url_logging_libraries()
