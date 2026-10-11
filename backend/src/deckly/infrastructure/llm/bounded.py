import asyncio
from dataclasses import dataclass

from deckly.infrastructure.llm.client import LlmClient, LlmPrompt, LlmReply


@dataclass(frozen=True, slots=True)
class BoundedLlmClient:
    inner: LlmClient
    gate: asyncio.Semaphore

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        async with self.gate:
            return await self.inner.complete(prompt)

    async def aclose(self) -> None:
        await self.inner.aclose()
