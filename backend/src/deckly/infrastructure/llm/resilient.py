from deckly.infrastructure.llm.client import LlmClient, LlmPrompt, LlmReply
from deckly.infrastructure.resilience import ResilientCaller


class ResilientLlmClient:
    def __init__(self, inner: LlmClient, caller: ResilientCaller) -> None:
        self._inner = inner
        self._caller = caller

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        return await self._caller.call(lambda: self._inner.complete(prompt))

    async def aclose(self) -> None:
        await self._inner.aclose()
