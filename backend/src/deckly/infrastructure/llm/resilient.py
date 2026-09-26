from deckly.infrastructure.llm.client import LlmClient, LlmPrompt, LlmReply
from deckly.infrastructure.resilience import CallSize, ResilientCaller

OVERSIZED_SHARE_OF_OUTPUT_BUDGET = 0.5


class ResilientLlmClient:
    def __init__(self, inner: LlmClient, caller: ResilientCaller, max_output_tokens: int) -> None:
        self._inner = inner
        self._caller = caller
        self._oversized_output_tokens = max_output_tokens * OVERSIZED_SHARE_OF_OUTPUT_BUDGET

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        return await self._caller.call(lambda: self._inner.complete(prompt), size=self._size_of(prompt))

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _size_of(self, prompt: LlmPrompt) -> CallSize:
        if prompt.expected_output_tokens >= self._oversized_output_tokens:
            return CallSize.OVERSIZED
        return CallSize.TYPICAL
