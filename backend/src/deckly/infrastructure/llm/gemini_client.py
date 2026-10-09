from collections.abc import Mapping
from urllib.parse import quote

import httpx2

from deckly.infrastructure.llm.client import (
    LlmEndpoint,
    LlmPrompt,
    LlmRejectedError,
    LlmReply,
    LlmResponseError,
    LlmStop,
    LlmUnavailableError,
    redact_credentials,
    status_error,
)
from deckly.infrastructure.resilience import RETRY_AFTER_HEADER, parse_retry_after

PROVIDER = "Gemini"
API_KEY_HEADER = "x-goog-api-key"
JSON_OUTPUT = "application/json"
THINKING_LEVEL = "low"
SINGLE_CANDIDATE = 1
UNSPECIFIED_BLOCK_REASON = "BLOCK_REASON_UNSPECIFIED"
SUSPENDED_ACCOUNT_FINISH_REASON = "PUP_LIMITED_DISABLED"
RETRY_INFO_TYPE = "type.googleapis.com/google.rpc.RetryInfo"
DURATION_UNIT = "s"
REFUSING_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "OTHER",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "ESCALATION",
    }
)
STOPS: Mapping[str, LlmStop] = {
    "MAX_TOKENS": LlmStop.TRUNCATED,
    **dict.fromkeys(REFUSING_FINISH_REASONS, LlmStop.REFUSED),
}
REFUSED_PROMPT = LlmReply(text="", stop=LlmStop.REFUSED)


def generate_content_path(model: str) -> str:
    return f"/models/{quote(model, safe='')}:generateContent"


def request_body(endpoint: LlmEndpoint, prompt: LlmPrompt) -> dict[str, object]:
    return {
        "systemInstruction": {"parts": [{"text": prompt.system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt.user}]}],
        "generationConfig": {
            "candidateCount": SINGLE_CANDIDATE,
            "maxOutputTokens": endpoint.max_output_tokens,
            "responseMimeType": JSON_OUTPUT,
            "thinkingConfig": {"thinkingLevel": THINKING_LEVEL},
        },
    }


def stop_of(finish: object) -> LlmStop:
    match finish:
        case None:
            return LlmStop.COMPLETE
        case str() if finish == SUSPENDED_ACCOUNT_FINISH_REASON:
            message = f"{PROVIDER} stopped serving the account: {finish}"
            raise LlmRejectedError(message)
        case str():
            return STOPS.get(finish, LlmStop.COMPLETE)
    message = f"{PROVIDER} answered with a finish reason that is not a string"
    raise LlmResponseError(message)


def part_text(part: object) -> str:
    match part:
        case {"thought": True}:
            return ""
        case {"text": str() as text}:
            return text
        case {"text": _}:
            message = f"{PROVIDER} answered with a text part that is not a string"
            raise LlmResponseError(message)
        case dict():
            return ""
    message = f"{PROVIDER} answered with a content part that is not an object"
    raise LlmResponseError(message)


def content_text(content: object) -> str:
    match content:
        case None:
            return ""
        case {"parts": list() as parts}:
            return "".join(part_text(part) for part in parts)
        case {"parts": _}:
            message = f"{PROVIDER} answered with content parts that are not a list"
            raise LlmResponseError(message)
        case dict():
            return ""
    message = f"{PROVIDER} answered with candidate content that is not an object"
    raise LlmResponseError(message)


def candidate_reply(candidate: Mapping[object, object]) -> LlmReply:
    stop = stop_of(candidate.get("finishReason"))
    return LlmReply(text=content_text(candidate.get("content")), stop=stop)


def reply_from(response: httpx2.Response) -> LlmReply:
    try:
        payload: object = response.json()
    except (ValueError, RecursionError) as error:
        message = f"{PROVIDER} answered with a body that is not JSON"
        raise LlmResponseError(message) from error
    match payload:
        case {"promptFeedback": {"blockReason": str() as reason}} if reason != UNSPECIFIED_BLOCK_REASON:
            return REFUSED_PROMPT
        case {"candidates": [dict() as candidate, *_]}:
            return candidate_reply(candidate)
    message = f"{PROVIDER} answered with no candidate"
    raise LlmResponseError(message)


def retry_delay(detail: object) -> str | None:
    match detail:
        case {"@type": str() as kind, "retryDelay": str() as delay} if (
            kind == RETRY_INFO_TYPE and delay.endswith(DURATION_UNIT)
        ):
            return delay.removesuffix(DURATION_UNIT)
    return None


def retry_hint(response: httpx2.Response) -> str | None:
    header = response.headers.get(RETRY_AFTER_HEADER)
    if parse_retry_after(header) is not None:
        return header
    try:
        payload: object = response.json()
    except (ValueError, RecursionError):
        return None
    match payload:
        case {"error": {"details": list() as details}}:
            return next((delay for detail in details if (delay := retry_delay(detail)) is not None), None)
    return None


class GeminiLlmClient:
    def __init__(self, endpoint: LlmEndpoint, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._endpoint = endpoint
        self._path = generate_content_path(endpoint.model)
        self._client = httpx2.AsyncClient(
            base_url=endpoint.base_url,
            headers={API_KEY_HEADER: endpoint.api_key},
            timeout=endpoint.timeout_seconds,
            transport=transport,
        )

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        try:
            response = await self._client.post(self._path, json=request_body(self._endpoint, prompt))
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
                retry_hint(response),
            )
        return reply_from(response)

    async def aclose(self) -> None:
        await self._client.aclose()
