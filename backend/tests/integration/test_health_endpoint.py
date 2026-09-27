import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from enum import StrEnum
from http import HTTPStatus
from uuid import uuid4

import httpx2
import pytest
from pydantic import PostgresDsn, RedisDsn

from deckly.config import Settings
from deckly.main import API_PREFIX, create_app
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ENDPOINT = f"{API_PREFIX}/health"
LOOPBACK = "127.0.0.1"
CHUNK_BYTES = 65536
DATABASE_HEALTH_TIMEOUT_SECONDS = 0.5
REDIS_TIMEOUT_SECONDS = 1
SAFETY_NET_SECONDS = 10
RECOVERY_POLL_SECONDS = 0.1


class Dependency(StrEnum):
    POSTGRES = "postgres"
    REDIS = "redis"


BOUND_SECONDS = {
    Dependency.POSTGRES: DATABASE_HEALTH_TIMEOUT_SECONDS,
    Dependency.REDIS: REDIS_TIMEOUT_SECONDS,
}


class Proxy:
    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._thawed = asyncio.Event()
        self._thawed.set()
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, LOOPBACK, 0)
        return int(self._server.sockets[0].getsockname()[1])

    def freeze(self) -> None:
        self._thawed.clear()

    def thaw(self) -> None:
        self._thawed.set()

    async def cut(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection(*self._upstream)
        self._writers += [writer, upstream_writer]
        await asyncio.gather(self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer))

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while data := await reader.read(CHUNK_BYTES):
                await self._thawed.wait()
                writer.write(data)
                await writer.drain()
        writer.close()


@dataclass(frozen=True, slots=True)
class Service:
    client: httpx2.AsyncClient
    proxies: dict[Dependency, Proxy]
    version: str


def postgres_at(settings: Settings, port: int) -> PostgresDsn:
    database_url = settings.database.url
    [database_host] = database_url.hosts()
    return PostgresDsn.build(
        scheme=database_url.scheme,
        username=database_host["username"],
        password=database_host["password"],
        host=LOOPBACK,
        port=port,
        path=(database_url.path or "").lstrip("/"),
    )


def through_proxies(settings: Settings, postgres_port: int, redis_port: int) -> Settings:
    redis_url = settings.redis.url
    return replace(
        settings,
        database=settings.database.model_copy(
            update={
                "url": postgres_at(settings, postgres_port),
                "health_check_timeout_seconds": DATABASE_HEALTH_TIMEOUT_SECONDS,
            }
        ),
        redis=settings.redis.model_copy(
            update={
                "url": RedisDsn.build(
                    scheme=redis_url.scheme,
                    username=redis_url.username,
                    password=redis_url.password,
                    host=LOOPBACK,
                    port=redis_port,
                    path=(redis_url.path or "").lstrip("/"),
                ),
                "connect_timeout_seconds": REDIS_TIMEOUT_SECONDS,
            }
        ),
    )


def upstream(settings: Settings, dependency: Dependency) -> Proxy:
    if dependency is Dependency.POSTGRES:
        [host] = settings.database.url.hosts()
        return Proxy(host["host"] or LOOPBACK, host["port"] or 0)
    return Proxy(settings.redis.url.host or LOOPBACK, settings.redis.url.port or 0)


@pytest.fixture
async def service(settings: Settings, restored_logging: None) -> AsyncIterator[Service]:
    del restored_logging
    proxies = {dependency: upstream(settings, dependency) for dependency in Dependency}
    ports = {dependency: await proxy.start() for dependency, proxy in proxies.items()}
    app = create_app(through_proxies(settings, ports[Dependency.POSTGRES], ports[Dependency.REDIS]))
    try:
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://deckly") as client,
        ):
            try:
                yield Service(client=client, proxies=proxies, version=settings.app.version)
            finally:
                for proxy in proxies.values():
                    proxy.thaw()
    finally:
        for proxy in proxies.values():
            await proxy.cut()


async def health(service: Service, headers: dict[str, str] | None = None) -> tuple[dict[str, object], float]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with asyncio.timeout(SAFETY_NET_SECONDS):
        response = await service.client.get(ENDPOINT, headers=headers or {})
    elapsed = loop.time() - started
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, object] = response.json()
    assert spec_errors("Health", body) == []
    return body, elapsed


async def until_ok(service: Service) -> dict[str, object]:
    async with asyncio.timeout(SAFETY_NET_SECONDS):
        while True:
            body, _ = await health(service)
            if body["status"] == "ok":
                return body
            await asyncio.sleep(RECOVERY_POLL_SECONDS)


async def test_health_is_ok_with_the_version_and_no_quota(service: Service) -> None:
    body, _ = await health(service)

    assert body == {"status": "ok", "version": service.version}


async def test_health_carries_the_quota_only_when_a_client_id_is_sent(service: Service) -> None:
    body, _ = await health(service, {"X-Client-Id": str(uuid4())})

    assert body["status"] == "ok"
    assert set(body) == {"status", "version", "quota"}


@pytest.mark.parametrize("dependency", list(Dependency))
async def test_frozen_dependency_makes_health_degraded_within_its_bound_and_it_recovers(
    service: Service, dependency: Dependency
) -> None:
    await until_ok(service)
    service.proxies[dependency].freeze()

    first, first_elapsed = await health(service)
    second, second_elapsed = await health(service)

    assert first["status"] == "degraded"
    assert first_elapsed < BOUND_SECONDS[dependency] + 1
    assert second["status"] == "degraded"
    assert second_elapsed < BOUND_SECONDS[dependency] + 1

    service.proxies[dependency].thaw()

    assert (await until_ok(service))["version"] == service.version


@pytest.mark.parametrize("dependency", list(Dependency))
async def test_unreachable_dependency_makes_health_degraded(service: Service, dependency: Dependency) -> None:
    await until_ok(service)
    await service.proxies[dependency].cut()

    body, elapsed = await health(service, {"X-Client-Id": str(uuid4())})

    assert body["status"] == "degraded"
    assert ("quota" in body) is (dependency is Dependency.POSTGRES)
    assert elapsed < BOUND_SECONDS[dependency] + 1


async def test_frozen_redis_leaves_the_quota_out_of_a_degraded_health_within_its_bound(
    service: Service,
) -> None:
    await until_ok(service)
    service.proxies[Dependency.REDIS].freeze()

    body, elapsed = await health(service, {"X-Client-Id": str(uuid4())})

    assert body == {"status": "degraded", "version": service.version}
    assert elapsed < REDIS_TIMEOUT_SECONDS + 1

    service.proxies[Dependency.REDIS].thaw()
    await until_ok(service)
    recovered, _ = await health(service, {"X-Client-Id": str(uuid4())})
    assert "quota" in recovered
