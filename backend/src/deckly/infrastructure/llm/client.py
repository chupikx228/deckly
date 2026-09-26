import math
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Protocol

from deckly.infrastructure.resilience import TransientError

TRANSIENT_STATUSES = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.CONFLICT, HTTPStatus.TOO_MANY_REQUESTS}
)
RETRY_AFTER_HEADER = "retry-after"
MAX_DETAIL_LENGTH = 500


class LlmStop(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class LlmPrompt:
    system: str
    user: str


@dataclass(frozen=True, slots=True)
class LlmReply:
    text: str
    stop: LlmStop


@dataclass(frozen=True, slots=True)
class LlmEndpoint:
    base_url: str
    api_key: str
    model: str
    max_output_tokens: int
    timeout_seconds: float


class LlmClient(Protocol):
    async def complete(self, prompt: LlmPrompt) -> LlmReply: ...

    async def aclose(self) -> None: ...


class LlmError(Exception):
    pass


class LlmUnavailableError(LlmError, TransientError):
    pass


class LlmRejectedError(LlmError):
    pass


class LlmResponseError(LlmError):
    pass


def is_transient_status(status: int) -> bool:
    return status in TRANSIENT_STATUSES or status >= HTTPStatus.INTERNAL_SERVER_ERROR


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def status_error(provider: str, status: int, detail: str, retry_after: str | None) -> LlmError:
    message = f"{provider} answered {status}: {detail[:MAX_DETAIL_LENGTH]}"
    if is_transient_status(status):
        return LlmUnavailableError(message, retry_after_seconds=parse_retry_after(retry_after))
    return LlmRejectedError(message)
