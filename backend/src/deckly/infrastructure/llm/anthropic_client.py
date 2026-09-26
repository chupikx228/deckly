from collections.abc import Mapping

import httpx2
from anthropic import APIConnectionError, APIResponseValidationError, APIStatusError, AsyncAnthropic
from anthropic.types import Message, TextBlock

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

PROVIDER = "Anthropic"
NO_SDK_RETRIES = 0
STOPS: Mapping[str, LlmStop] = {
    "max_tokens": LlmStop.TRUNCATED,
    "model_context_window_exceeded": LlmStop.TRUNCATED,
    "refusal": LlmStop.REFUSED,
}


def reply_from(message: Message) -> LlmReply:
    text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
    return LlmReply(text=text, stop=STOPS.get(message.stop_reason or "", LlmStop.COMPLETE))


class AnthropicLlmClient:
    def __init__(self, endpoint: LlmEndpoint, http_client: httpx2.AsyncClient | None = None) -> None:
        self._endpoint = endpoint
        self._client = AsyncAnthropic(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
            timeout=endpoint.timeout_seconds,
            max_retries=NO_SDK_RETRIES,
            http_client=http_client,
        )

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        try:
            answer = await self._client.messages.create(
                model=self._endpoint.model,
                max_tokens=self._endpoint.max_output_tokens,
                system=prompt.system,
                messages=[{"role": "user", "content": prompt.user}],
            )
        except APIStatusError as error:
            raise status_error(
                PROVIDER, error.status_code, error.message, error.response.headers.get(RETRY_AFTER_HEADER)
            ) from error
        except APIConnectionError as error:
            message = f"{PROVIDER} could not be reached: {error.message}"
            raise LlmUnavailableError(message) from error
        except APIResponseValidationError as error:
            message = f"{PROVIDER} answered with an unexpected shape: {error.message}"
            raise LlmResponseError(message) from error
        return reply_from(answer)

    async def aclose(self) -> None:
        await self._client.close()
