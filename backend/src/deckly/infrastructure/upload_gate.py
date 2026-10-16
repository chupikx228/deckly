import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from deckly.application.exceptions import UploadCapacityExceededError
from deckly.infrastructure.observability.metrics import GateWait, Metrics

UPLOAD_CAPACITY_RETRY_AFTER_SECONDS = 5
NO_WAIT_SECONDS = 0.0


@dataclass(frozen=True, slots=True)
class RejectingUploadGate:
    gate: asyncio.Semaphore
    gate_name: str
    metrics: Metrics

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        if self.gate.locked():
            self.metrics.gate_rejections.labels(gate=self.gate_name).inc()
            raise UploadCapacityExceededError(UPLOAD_CAPACITY_RETRY_AFTER_SECONDS)
        await self.gate.acquire()
        try:
            self.metrics.gate_wait.labels(gate=self.gate_name, outcome=GateWait.ACQUIRED).observe(
                NO_WAIT_SECONDS
            )
            in_flight = self.metrics.gate_in_flight.labels(gate=self.gate_name)
            in_flight.inc()
            try:
                yield
            finally:
                in_flight.dec()
        finally:
            self.gate.release()
