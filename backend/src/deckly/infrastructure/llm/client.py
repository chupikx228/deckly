from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from deckly.infrastructure.resilience import TransientError, is_transient_status, parse_retry_after

MAX_DETAIL_LENGTH = 500


class LlmStop(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class LlmPrompt:
    system: str
    user: str
    expected_output_tokens: int


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


def status_error(provider: str, status: int, detail: str, retry_after: str | None) -> LlmError:
    message = f"{provider} answered {status}: {detail[:MAX_DETAIL_LENGTH]}"
    if is_transient_status(status):
        return LlmUnavailableError(message, retry_after_seconds=parse_retry_after(retry_after))
    return LlmRejectedError(message)
