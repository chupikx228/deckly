import asyncio
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from deckly.application.generations import Clock
from deckly.application.ports import DependencyProbe, Quota, QuotaReader


class ServiceStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: ServiceStatus
    version: str
    quota: Quota | None


@dataclass(frozen=True, slots=True)
class CheckHealth:
    probes: tuple[DependencyProbe, ...]
    quota: QuotaReader
    clock: Clock
    version: str

    async def __call__(self, client_id: UUID | None) -> HealthReport:
        healthy = await asyncio.gather(*(probe.is_healthy() for probe in self.probes))
        return HealthReport(
            status=ServiceStatus.OK if all(healthy) else ServiceStatus.DEGRADED,
            version=self.version,
            quota=None if client_id is None else await self.quota.current(client_id, self.clock()),
        )
