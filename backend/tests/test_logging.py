import json
import logging

from deckly.infrastructure.logging import JsonFormatter


def make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord("deckly", logging.ERROR, __file__, 1, message, None, None)


def test_extra_fields_are_included() -> None:
    record = make_record("stage_changed")
    record.job_id = "job-1"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["job_id"] == "job-1"


def test_extra_fields_cannot_override_core_fields() -> None:
    record = make_record("real message")
    record.level = "DEBUG"
    record.timestamp = "forged"
    record.logger = "forged"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "ERROR"
    assert payload["logger"] == "deckly"
    assert payload["timestamp"] != "forged"
    assert payload["message"] == "real message"
