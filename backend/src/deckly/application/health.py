import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
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
class QuotaReading:
    readable: bool
    quota: Quota | None


UNREQUESTED = QuotaReading(readable=True, quota=None)
UNREADABLE = QuotaReading(readable=False, quota=None)


@dataclass(frozen=True, slots=True)
class CheckHealth:
    probes: tuple[DependencyProbe, ...]
    quota: QuotaReader
    clock: Clock
    version: str

    async def __call__(self, client_id: UUID | None) -> HealthReport:
        healthy, reading = await asyncio.gather(
            asyncio.gather(*(probe.is_healthy() for probe in self.probes)),
            self._read_quota(client_id, self.clock()),
        )
        return HealthReport(
            status=ServiceStatus.OK if all(healthy) and reading.readable else ServiceStatus.DEGRADED,
            version=self.version,
            quota=reading.quota,
        )

    async def _read_quota(self, client_id: UUID | None, now: datetime) -> QuotaReading:
        if client_id is None:
            return UNREQUESTED
        try:
            return QuotaReading(readable=True, quota=await self.quota.current(client_id, now))
        except UpstreamUnavailableError:
            return UNREADABLE
