import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from deckly.infrastructure.llm.client import LlmClient, LlmPrompt, LlmReply
from deckly.infrastructure.observability.metrics import GateWait, Metrics


@dataclass(frozen=True, slots=True)
class BoundedLlmClient:
    inner: LlmClient
    gate: asyncio.Semaphore
    gate_name: str
    metrics: Metrics
    clock: Callable[[], float]

    async def complete(self, prompt: LlmPrompt) -> LlmReply:
        await self._acquire()
        try:
            in_flight = self.metrics.gate_in_flight.labels(gate=self.gate_name)
            in_flight.inc()
            try:
                return await self.inner.complete(prompt)
            finally:
                in_flight.dec()
        finally:
            self.gate.release()

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def _acquire(self) -> None:
        queued_at = self.clock()
        try:
            await self.gate.acquire()
        except asyncio.CancelledError:
            self._observe_wait(GateWait.ABANDONED, queued_at)
            raise
        self._observe_wait(GateWait.ACQUIRED, queued_at)

    def _observe_wait(self, outcome: GateWait, queued_at: float) -> None:
        self.metrics.gate_wait.labels(gate=self.gate_name, outcome=outcome).observe(self.clock() - queued_at)
