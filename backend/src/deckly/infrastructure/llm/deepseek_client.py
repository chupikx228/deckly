import re
from collections.abc import Mapping

import httpx2

from deckly.infrastructure.llm.client import (
    LlmEndpoint,
    LlmPrompt,
    LlmReply,
    LlmResponseError,
    LlmStop,
    LlmUnavailableError,
    status_error,
)
from deckly.infrastructure.resilience import RETRY_AFTER_HEADER

PROVIDER = "DeepSeek"
CHAT_COMPLETIONS_PATH = "/chat/completions"
AUTHORIZATION_HEADER = "Authorization"
JSON_OUTPUT: Mapping[str, str] = {"type": "json_object"}
TRANSIENT_FINISH_REASONS = frozenset({"insufficient_system_resource", "aborted"})
STOPS: Mapping[str, LlmStop] = {"length": LlmStop.TRUNCATED, "content_filter": LlmStop.REFUSED}
MASKED_CREDENTIAL = re.compile(r"(?<![\w*-])[\w-]*\*{3,}[\w*-]*")
REDACTED = "[redacted]"


def redact_credentials(text: str, api_key: str) -> str:
    unmasked = text.replace(api_key, REDACTED) if api_key else text
    return MASKED_CREDENTIAL.sub(REDACTED, unmasked)


def request_body(endpoint: LlmEndpoint, prompt: LlmPrompt) -> dict[str, object]:
    return {
        "model": endpoint.model,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "max_tokens": endpoint.max_output_tokens,
        "response_format": dict(JSON_OUTPUT),
        "stream": False,
    }


def completed(finish: str | None, content: str | None) -> LlmReply:
    if finish in TRANSIENT_FINISH_REASONS:
        message = f"{PROVIDER} could not finish the completion: {finish}"
        raise LlmUnavailableError(message)
    return LlmReply(text=content or "", stop=STOPS.get(finish or "", LlmStop.COMPLETE))


def reply_from(response: httpx2.Response) -> LlmReply:
    if not response.content.strip():
        message = f"{PROVIDER} closed the connection before starting the completion"
        raise LlmUnavailableError(message)
    try:
        payload: object = response.json()
    except ValueError as error:
        message = f"{PROVIDER} answered with a body that is not JSON"
        raise LlmResponseError(message) from error
    match payload:
        case {
            "choices": [
                {"finish_reason": str() | None as finish, "message": {"content": str() | None as content}},
                *_,
            ]
        }:
            return completed(finish, content)
    message = f"{PROVIDER} answered without a completion choice"
    raise LlmResponseError(message)


class DeepSeekLlmClient:
    def __init__(self, endpoint: LlmEndpoint, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._endpoint = endpoint
        self._client = httpx2.AsyncClient(
            base_url=endpoint.base_url,
            headers={AUTHORIZATION_HEADER: f"Bearer {endpoint.api_key}"},
            timeout=endpoint.timeout_seconds,
            transport=transport,
        )

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        try:
            response = await self._client.post(
                CHAT_COMPLETIONS_PATH, json=request_body(self._endpoint, prompt)
            )
        except httpx2.TransportError as error:
            message = f"{PROVIDER} could not be reached: {type(error).__name__}"
            raise LlmUnavailableError(message) from error
        except httpx2.HTTPError as error:
            message = f"{PROVIDER} request failed: {type(error).__name__}"
            raise LlmResponseError(message) from error
        if not response.is_success:
            raise status_error(
                PROVIDER,
                response.status_code,
                redact_credentials(response.text, self._endpoint.api_key),
                response.headers.get(RETRY_AFTER_HEADER),
            )
        return reply_from(response)

    async def aclose(self) -> None:
        await self._client.aclose()
