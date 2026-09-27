import asyncio
from datetime import datetime
from uuid import UUID

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.health import CheckHealth, ServiceStatus
from deckly.application.ports import Quota, QuotaReader
from deckly.infrastructure.quota import QUOTA_WINDOW
from tests.domain.builders import T0, client_id
from tests.fakes import QUOTA_LIMIT, InMemoryQuota

pytestmark = pytest.mark.anyio

VERSION = "1.2.3"


class GatedProbe:
    def __init__(self, gate: asyncio.Event, *, healthy: bool) -> None:
        self.gate = gate
        self.healthy = healthy
        self.started = asyncio.Event()

    async def is_healthy(self) -> bool:
        self.started.set()
        await self.gate.wait()
        return self.healthy


def check_health(*probes: GatedProbe, quota: QuotaReader | None = None) -> CheckHealth:
    return CheckHealth(
        probes=probes, quota=InMemoryQuota() if quota is None else quota, clock=lambda: T0, version=VERSION
    )


def opened(*, healthy: bool) -> GatedProbe:
    gate = asyncio.Event()
    gate.set()
    return GatedProbe(gate, healthy=healthy)


async def test_all_healthy_dependencies_report_ok_with_the_version() -> None:
    report = await check_health(opened(healthy=True), opened(healthy=True))(None)

    assert (report.status, report.version, report.quota) == (ServiceStatus.OK, VERSION, None)


@pytest.mark.parametrize("healthy", [(False, True), (True, False), (False, False)])
async def test_any_unhealthy_dependency_reports_degraded(healthy: tuple[bool, bool]) -> None:
    report = await check_health(*(opened(healthy=each) for each in healthy))(None)

    assert report.status is ServiceStatus.DEGRADED


async def test_quota_is_read_only_for_a_client() -> None:
    report = await check_health(opened(healthy=True))(client_id(1))

    assert report.quota == Quota(limit=QUOTA_LIMIT, remaining=QUOTA_LIMIT, resets_at=T0 + QUOTA_WINDOW)


async def test_degraded_service_still_reports_the_quota() -> None:
    report = await check_health(opened(healthy=False))(client_id(1))

    assert report.status is ServiceStatus.DEGRADED
    assert report.quota is not None


async def test_dependencies_are_checked_concurrently() -> None:
    gate = asyncio.Event()
    probes = (GatedProbe(gate, healthy=True), GatedProbe(gate, healthy=True))
    checking = asyncio.create_task(check_health(*probes)(None))

    await asyncio.wait_for(asyncio.gather(*(probe.started.wait() for probe in probes)), timeout=1)
    gate.set()

    assert (await checking).status is ServiceStatus.OK


def unreadable_quota() -> InMemoryQuota:
    quota = InMemoryQuota()
    quota.failure = UpstreamUnavailableError(1)
    return quota


async def test_unreadable_quota_leaves_it_out_and_reports_degraded() -> None:
    report = await check_health(opened(healthy=True), quota=unreadable_quota())(client_id(1))

    assert (report.status, report.quota) == (ServiceStatus.DEGRADED, None)


async def test_unreadable_quota_is_not_read_without_a_client() -> None:
    report = await check_health(opened(healthy=True), quota=unreadable_quota())(None)

    assert (report.status, report.quota) == (ServiceStatus.OK, None)


async def test_unexpected_quota_failure_is_not_mistaken_for_an_outage() -> None:
    quota = InMemoryQuota()
    quota.failure = TypeError("redis answered GET with a list")

    with pytest.raises(TypeError):
        await check_health(opened(healthy=True), quota=quota)(client_id(1))


class GatedQuota(InMemoryQuota):
    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate
        self.started = asyncio.Event()

    async def current(self, client_id: UUID, now: datetime) -> Quota:
        self.started.set()
        await self.gate.wait()
        return await super().current(client_id, now)


async def test_quota_is_read_concurrently_with_the_dependencies() -> None:
    gate = asyncio.Event()
    probe = GatedProbe(gate, healthy=True)
    quota = GatedQuota(gate)
    checking = asyncio.create_task(check_health(probe, quota=quota)(client_id(1)))

    await asyncio.wait_for(asyncio.gather(probe.started.wait(), quota.started.wait()), timeout=1)
    gate.set()

    assert (await checking).quota is not None
