import asyncio
import base64
from dataclasses import replace
from ipaddress import IPv4Address

import pytest

from deckly.infrastructure.recording_url.egress import (
    ESTABLISHED,
    EgressProxy,
    EgressRefusal,
    EgressSubject,
    PublicHostPolicy,
    connect,
    fully_qualified,
    public_address,
    resolve,
)
from tests.fakes import fresh_metrics
from tests.recording_url import (
    ALLOWED_HOST,
    MEDIA_HOST,
    PUBLIC_ADDRESS,
    TEST_LIMITS,
    TEST_POLICY,
    LoopbackConnector,
    ScriptedResolver,
    close,
    connect_head,
    listening,
    resolving,
    send_head,
    tunnel,
)

pytestmark = pytest.mark.anyio

ESTABLISHED_STATUS = ESTABLISHED.split(b"\r\n")[0]
REFUSED_STATUS = b"HTTP/1.1 403 Forbidden"
UNREACHABLE_STATUS = b"HTTP/1.1 502 Bad Gateway"
TLS_TARGET = f"{ALLOWED_HOST}:443"


def proxy_with(resolver: ScriptedResolver, connector: LoopbackConnector | None = None) -> EgressProxy:
    if connector is None:
        return EgressProxy(TEST_POLICY, TEST_LIMITS, fresh_metrics(), resolver=resolver, connector=connect)
    return EgressProxy(TEST_POLICY, TEST_LIMITS, fresh_metrics(), resolver=resolver, connector=connector)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.8.9.10",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        str(IPv4Address(0)),
        "255.255.255.255",
        "224.0.0.251",
        "192.0.2.1",
        "198.18.0.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "2001:0:4136:e378:8000:63bf:80ff:fffe",
        "::7f00:1",
        "::a9fe:a9fe",
        "::ffff:0:7f00:1",
        "fec0::1",
        "100::1",
        "2001:db8::1",
        "64:ff9b:1::a9fe:a9fe",
        "168.63.129.16",
        "::ffff:168.63.129.16",
        "64:ff9b::a83f:8110",
        "not-an-address",
        "",
    ],
)
def test_internal_special_and_unparseable_addresses_are_not_public(address: str) -> None:
    assert public_address(address) is None


@pytest.mark.parametrize(
    "address", [PUBLIC_ADDRESS, "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"]
)
def test_ordinary_internet_addresses_are_public(address: str) -> None:
    assert public_address(address) is not None


@pytest.mark.parametrize("internal", ["127.0.0.1", "169.254.169.254", "10.1.2.3", "::1", "::ffff:127.0.0.1"])
async def test_allowlisted_host_resolving_to_an_internal_address_never_reaches_it(internal: str) -> None:
    async with listening() as service:
        resolver = resolving(ALLOWED_HOST, [internal])
        proxy = EgressProxy(
            replace(TEST_POLICY, port=service.port), TEST_LIMITS, fresh_metrics(), resolver=resolver
        )
        async with proxy.serve() as session:
            status, streams = await tunnel(session.proxy_url, f"{ALLOWED_HOST}:{service.port}")
            await close(streams)

        assert status == REFUSED_STATUS
        assert session.refusals == [EgressRefusal.ADDRESS]
        assert service.connections == 0


async def test_positive_control_a_public_answer_is_dialled_so_the_zero_connection_checks_mean_something() -> (
    None
):
    async with listening() as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        connector = LoopbackConnector(service.port)
        async with proxy_with(resolver, connector).serve() as session:
            status, streams = await tunnel(session.proxy_url, TLS_TARGET)
            await close(streams)
            await asyncio.sleep(0.05)

        assert status == ESTABLISHED_STATUS
        assert session.refusals == []
        assert connector.dialled == [PUBLIC_ADDRESS]
        assert service.connections == 1


async def test_any_internal_address_among_the_answers_refuses_the_host() -> None:
    connector = LoopbackConnector(port=1)
    resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS, "10.0.0.1"])
    async with proxy_with(resolver, connector).serve() as session:
        status, streams = await tunnel(session.proxy_url, TLS_TARGET)
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals == [EgressRefusal.ADDRESS]
    assert connector.dialled == []


async def test_dns_rebinding_cannot_move_a_tunnel_after_its_address_was_checked() -> None:
    async with listening() as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS], ["127.0.0.1"])
        connector = LoopbackConnector(service.port)
        async with proxy_with(resolver, connector).serve() as session:
            first, first_streams = await tunnel(session.proxy_url, TLS_TARGET)
            second, second_streams = await tunnel(session.proxy_url, TLS_TARGET)
            await close(first_streams)
            await close(second_streams)

        assert first == ESTABLISHED_STATUS
        assert second == REFUSED_STATUS
        assert resolver.calls == [ALLOWED_HOST, ALLOWED_HOST]
        assert connector.dialled == [PUBLIC_ADDRESS]
        assert session.refusals == [EgressRefusal.ADDRESS]


async def test_redirect_chain_cannot_leave_the_allowlist_or_reach_an_internal_address() -> None:
    resolver = ScriptedResolver({ALLOWED_HOST: [[PUBLIC_ADDRESS]], MEDIA_HOST: [["169.254.169.254"]]})
    async with listening() as service:
        connector = LoopbackConnector(service.port)
        async with proxy_with(resolver, connector).serve() as session:
            hops = [
                await tunnel(session.proxy_url, TLS_TARGET),
                await tunnel(session.proxy_url, "metadata.google.internal:443"),
                await tunnel(session.proxy_url, "169.254.169.254:443"),
                await tunnel(session.proxy_url, f"{MEDIA_HOST}:443"),
                await send_head(
                    session.proxy_url,
                    connect_head("http://169.254.169.254/latest/meta-data/", session.proxy_url, method="GET"),
                ),
            ]
            for _, streams in hops:
                await close(streams)

        assert [status for status, _ in hops] == [ESTABLISHED_STATUS, *[REFUSED_STATUS] * 4]
        assert session.refusals == [
            EgressRefusal.HOST,
            EgressRefusal.HOST,
            EgressRefusal.ADDRESS,
            EgressRefusal.METHOD,
        ]
        assert resolver.calls == [ALLOWED_HOST, MEDIA_HOST]
        assert connector.dialled == [PUBLIC_ADDRESS]
        assert service.connections == 1


@pytest.mark.parametrize(
    "target",
    [
        "www.youtube.com.evil.example:443",
        "evil.example:443",
        "googlevideo.com:443",
        "youtube.com:443",
        "127.0.0.1:443",
        "[::1]:443",
        "www.youtube.com",
        "www.youtube.com:443:443",
        "www.youtube.com@evil.example:443",
    ],
)
async def test_hosts_off_the_allowlist_are_refused_without_being_resolved(target: str) -> None:
    resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
    async with proxy_with(resolver).serve() as session:
        status, streams = await tunnel(session.proxy_url, target)
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals in ([EgressRefusal.HOST], [EgressRefusal.MALFORMED])
    assert resolver.calls == []


@pytest.mark.parametrize("port", [80, 8443, 22, 6379, 0])
async def test_ports_other_than_tls_are_refused(port: int) -> None:
    resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
    async with proxy_with(resolver).serve() as session:
        status, streams = await tunnel(session.proxy_url, f"{ALLOWED_HOST}:{port}")
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals == [EgressRefusal.PORT]
    assert resolver.calls == []


async def test_upper_case_host_is_matched_after_lowering() -> None:
    async with listening() as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        async with proxy_with(resolver, LoopbackConnector(service.port)).serve() as session:
            status, streams = await tunnel(session.proxy_url, "WWW.YouTube.COM:443")
            await close(streams)

    assert status == ESTABLISHED_STATUS


@pytest.mark.parametrize("credentials", [None, "deckly:wrong", "someone:else"])
async def test_tunnel_without_this_fetchs_credentials_is_refused(credentials: str | None) -> None:
    resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
    async with proxy_with(resolver).serve() as session:
        lines = [f"CONNECT {TLS_TARGET} HTTP/1.1"]
        if credentials is not None:
            lines.append(f"Proxy-Authorization: Basic {base64.b64encode(credentials.encode()).decode()}")
        status, streams = await send_head(session.proxy_url, ("\r\n".join(lines) + "\r\n\r\n").encode())
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals == [EgressRefusal.CREDENTIALS]
    assert resolver.calls == []


async def test_unresolvable_host_is_refused() -> None:
    resolver = ScriptedResolver({})
    async with proxy_with(resolver).serve() as session:
        status, streams = await tunnel(session.proxy_url, TLS_TARGET)
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals == [EgressRefusal.RESOLUTION]


async def test_unreachable_address_is_a_bad_gateway() -> None:
    async def unreachable(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise ConnectionRefusedError(address, port)

    proxy = EgressProxy(
        TEST_POLICY,
        TEST_LIMITS,
        fresh_metrics(),
        resolver=resolving(ALLOWED_HOST, [PUBLIC_ADDRESS]),
        connector=unreachable,
    )
    async with proxy.serve() as session:
        status, streams = await tunnel(session.proxy_url, TLS_TARGET)
        await close(streams)

    assert status == UNREACHABLE_STATUS
    assert session.refusals == [EgressRefusal.UNREACHABLE]


async def test_bytes_past_the_fetch_budget_cut_the_tunnel() -> None:
    oversized = b"x" * (TEST_LIMITS.max_bytes + 1)
    async with listening(reply=oversized) as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        async with proxy_with(resolver, LoopbackConnector(service.port)).serve() as session:
            status, (reader, writer) = await tunnel(session.proxy_url, TLS_TARGET)
            received = await reader.read()
            await close((reader, writer))

    assert status == ESTABLISHED_STATUS
    assert len(received) <= TEST_LIMITS.max_bytes
    assert session.refusals == [EgressRefusal.BYTES]


async def test_byte_budget_is_shared_by_every_tunnel_of_one_fetch() -> None:
    half = b"x" * (TEST_LIMITS.max_bytes // 2 + 1)
    async with listening(reply=half) as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        async with proxy_with(resolver, LoopbackConnector(service.port)).serve() as session:
            _, first = await tunnel(session.proxy_url, TLS_TARGET)
            await first[0].readexactly(len(half))
            _, second = await tunnel(session.proxy_url, TLS_TARGET)
            await asyncio.wait_for(second[0].read(), timeout=2)
            for streams in (first, second):
                await close(streams)

    assert EgressRefusal.BYTES in session.refusals


async def test_tunnels_past_the_per_fetch_limit_are_refused() -> None:
    async with listening() as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        async with proxy_with(resolver, LoopbackConnector(service.port)).serve() as session:
            statuses = []
            for _ in range(TEST_LIMITS.max_tunnels + 1):
                status, streams = await tunnel(session.proxy_url, TLS_TARGET)
                statuses.append(status)
                await close(streams)

    assert statuses[:-1] == [ESTABLISHED_STATUS] * TEST_LIMITS.max_tunnels
    assert statuses[-1] == REFUSED_STATUS
    assert session.refusals == [EgressRefusal.TUNNELS]


async def test_request_head_that_never_ends_or_is_too_large_is_refused() -> None:
    resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
    async with proxy_with(resolver).serve() as session:
        silent_status, silent = await send_head(
            session.proxy_url, b"CONNECT www.youtube.com:443 HTTP/1.1\r\n"
        )
        huge_status, huge = await send_head(session.proxy_url, b"CONNECT " + b"a" * 10_000 + b"\r\n\r\n")
        garbage_status, garbage = await send_head(session.proxy_url, b"\r\n\r\n")
        for streams in (silent, huge, garbage):
            await close(streams)

    assert [silent_status, huge_status, garbage_status] == [REFUSED_STATUS] * 3
    assert session.refusals == [EgressRefusal.MALFORMED] * 3
    assert resolver.calls == []


async def test_tunnel_carries_bytes_both_ways() -> None:
    async with listening() as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        async with proxy_with(resolver, LoopbackConnector(service.port)).serve() as session:
            _, (reader, writer) = await tunnel(session.proxy_url, TLS_TARGET)
            writer.write(b"hello through the tunnel")
            await writer.drain()
            echoed = await reader.readexactly(len(b"hello through the tunnel"))
            await close((reader, writer))

    assert echoed == b"hello through the tunnel"
    assert session.hosts == [ALLOWED_HOST]


async def test_proxy_stops_listening_and_drops_open_tunnels_when_the_fetch_ends() -> None:
    async with listening() as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        async with proxy_with(resolver, LoopbackConnector(service.port)).serve() as session:
            _, (reader, writer) = await tunnel(session.proxy_url, TLS_TARGET)
            proxy_url = session.proxy_url

        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        await close((reader, writer))
        with pytest.raises(ConnectionRefusedError):
            await send_head(proxy_url, connect_head(TLS_TARGET, proxy_url))


async def test_every_refusal_is_counted_by_reason() -> None:
    metrics = fresh_metrics()
    proxy = EgressProxy(TEST_POLICY, TEST_LIMITS, metrics, resolver=resolving(ALLOWED_HOST, ["127.0.0.1"]))
    async with proxy.serve() as session:
        for target in (TLS_TARGET, "evil.example:443"):
            _, streams = await tunnel(session.proxy_url, target)
            await close(streams)

    for reason in (EgressRefusal.ADDRESS, EgressRefusal.HOST):
        assert (
            metrics.registry.get_sample_value(
                "deckly_recording_url_egress_refusals_total", {"reason": reason}
            )
            == 1
        )


def test_media_hosts_match_the_suffix_but_the_bare_domain_does_not() -> None:
    assert TEST_POLICY.allows(MEDIA_HOST)
    assert TEST_POLICY.allows(ALLOWED_HOST)
    assert not TEST_POLICY.allows("googlevideo.com")
    assert not TEST_POLICY.allows("evilgooglevideo.com")


async def test_tunnel_carrying_exactly_the_byte_budget_is_not_cut() -> None:
    exact = b"x" * TEST_LIMITS.max_bytes
    async with listening(reply=exact) as service:
        resolver = resolving(ALLOWED_HOST, [PUBLIC_ADDRESS])
        async with proxy_with(resolver, LoopbackConnector(service.port)).serve() as session:
            status, (reader, writer) = await tunnel(session.proxy_url, TLS_TARGET)
            received = await reader.readexactly(len(exact))
            await close((reader, writer))

    assert status == ESTABLISHED_STATUS
    assert received == exact
    assert session.refusals == []


async def test_host_that_resolves_to_no_address_is_refused() -> None:
    resolver = ScriptedResolver({ALLOWED_HOST: [[]]})
    async with proxy_with(resolver).serve() as session:
        status, streams = await tunnel(session.proxy_url, TLS_TARGET)
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals == [EgressRefusal.RESOLUTION]


PUBLIC_POLICY = PublicHostPolicy()
ARTICLE_HOST = "news.example.com"


def article_proxy(resolver: ScriptedResolver, connector: LoopbackConnector) -> EgressProxy:
    return EgressProxy(
        PUBLIC_POLICY,
        TEST_LIMITS,
        fresh_metrics(),
        resolver=resolver,
        connector=connector,
        subject=EgressSubject.ARTICLE_URL,
    )


@pytest.mark.parametrize("host", [ARTICLE_HOST, "en.wikipedia.org", "xn--d1acufc.xn--p1ai", "1.2.3.4.nip.io"])
def test_public_host_policy_allows_any_public_domain_name(host: str) -> None:
    assert PUBLIC_POLICY.allows(host)
    assert PUBLIC_POLICY.port == 443


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "169.254.169.254",
        "2130706433",
        "0x7f.0.0.1",
        "017700000001",
        "127.1",
        "localhost",
        "api.localhost",
        "metadata.google.internal",
        "printer.local",
        "kubernetes",
        "example.com.",
        "1.0.0.127.in-addr.arpa",
    ],
)
def test_public_host_policy_refuses_addresses_numeric_forms_and_local_names(host: str) -> None:
    assert not PUBLIC_POLICY.allows(host)


@pytest.mark.parametrize(
    "target",
    [
        "2130706433:443",
        "0x7f.1:443",
        "127.0.0.1:443",
        "localhost:443",
        "metadata.google.internal:443",
        "[::1]:443",
    ],
)
async def test_article_proxy_refuses_numeric_and_local_targets_before_any_dns_lookup(target: str) -> None:
    resolver = ScriptedResolver({})
    connector = LoopbackConnector(port=1)
    proxy = article_proxy(resolver, connector)
    async with proxy.serve() as session:
        status, streams = await tunnel(session.proxy_url, target)
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals in ([EgressRefusal.HOST], [EgressRefusal.MALFORMED])
    assert resolver.calls == []
    assert connector.dialled == []


async def test_article_proxy_only_tunnels_to_the_https_port() -> None:
    resolver = resolving(ARTICLE_HOST, [PUBLIC_ADDRESS])
    connector = LoopbackConnector(port=1)
    async with article_proxy(resolver, connector).serve() as session:
        status, streams = await tunnel(session.proxy_url, f"{ARTICLE_HOST}:8443")
        await close(streams)

    assert status == REFUSED_STATUS
    assert session.refusals == [EgressRefusal.PORT]
    assert resolver.calls == []


@pytest.mark.parametrize("internal", ["127.0.0.1", "169.254.169.254", "168.63.129.16", "fd00:ec2::254"])
async def test_article_proxy_never_dials_an_arbitrary_host_that_resolves_internally(internal: str) -> None:
    async with listening() as service:
        connector = LoopbackConnector(service.port)
        async with article_proxy(resolving(ARTICLE_HOST, [internal]), connector).serve() as session:
            status, streams = await tunnel(session.proxy_url, f"{ARTICLE_HOST}:443")
            await close(streams)

    assert status == REFUSED_STATUS
    assert connector.dialled == []
    assert service.connections == 0


async def test_article_proxy_counts_and_logs_its_refusals_under_its_own_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = fresh_metrics()
    proxy = EgressProxy(
        PUBLIC_POLICY,
        TEST_LIMITS,
        metrics,
        resolver=resolving(ARTICLE_HOST, ["10.0.0.1"]),
        subject=EgressSubject.ARTICLE_URL,
    )
    async with proxy.serve() as session:
        _, streams = await tunnel(session.proxy_url, f"{ARTICLE_HOST}:443")
        await close(streams)

    labels: dict[str, str] = {"reason": EgressRefusal.ADDRESS}
    assert metrics.registry.get_sample_value("deckly_article_url_egress_refusals_total", labels) == 1
    assert metrics.registry.get_sample_value("deckly_recording_url_egress_refusals_total", labels) is None
    assert [record.message for record in caplog.records] == ["article_url_egress_refused"]


@pytest.mark.parametrize(
    ("host", "absolute"), [("example.com", "example.com."), ("example.com.", "example.com.")]
)
def test_names_are_resolved_as_fully_qualified_so_search_domains_never_apply(
    host: str, absolute: str
) -> None:
    assert fully_qualified(host) == absolute


async def test_resolver_asks_for_the_fully_qualified_name(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    loop = asyncio.get_running_loop()

    async def recording_getaddrinfo(
        host: str, port: int, **options: int
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del options
        asked.append(host)
        return [(2, 1, 6, "", (PUBLIC_ADDRESS, port))]

    monkeypatch.setattr(loop, "getaddrinfo", recording_getaddrinfo)

    assert await resolve("kubernetes.default", 443) == [PUBLIC_ADDRESS]
    assert asked == ["kubernetes.default."]
