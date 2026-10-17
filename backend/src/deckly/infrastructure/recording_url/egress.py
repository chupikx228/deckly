import asyncio
import base64
import binascii
import hmac
import ipaddress
import logging
import re
import secrets
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum

from deckly.infrastructure.observability.metrics import Metrics

logger = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"
ANY_PORT = 0
TLS_PORT = 443
HEAD_END = b"\r\n\r\n"
LINE_END = "\r\n"
CONNECT_METHOD = "CONNECT"
PROXY_USER = "deckly"
PROXY_AUTHORIZATION = "proxy-authorization"
BASIC_SCHEME = "basic"
HEAD_ENCODING = "latin-1"
TOKEN_BYTES = 24
COPY_CHUNK_BYTES = 64 * 1024
MAX_HEAD_BYTES = 8 * 1024
ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
REFUSED = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
UNREACHABLE = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
TARGET = re.compile(r"(?P<host>[a-z0-9.-]{1,253}):(?P<port>[0-9]{1,5})")
REQUEST_LINE_PARTS = 3
HEADER_SEPARATOR = ":"
NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
IPV4_COMPATIBLE_PREFIX = ipaddress.IPv6Network("::/96")
IPV4_TRANSLATED_PREFIX = ipaddress.IPv6Network("::ffff:0:0:0/96")
GLOBAL_UNICAST = ipaddress.IPv6Network("2000::/3")
IPV4_BITS = 0xFFFF_FFFF

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = Callable[[str, int], Awaitable[list[str]]]
type Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]
type Connector = Callable[[str, int], Awaitable[Streams]]


class EgressRefusal(StrEnum):
    MALFORMED = "malformed"
    METHOD = "method"
    CREDENTIALS = "credentials"
    PORT = "port"
    HOST = "host"
    TUNNELS = "tunnels"
    RESOLUTION = "resolution"
    ADDRESS = "address"
    UNREACHABLE = "unreachable"
    BYTES = "bytes"


class RefusedError(Exception):
    def __init__(self, reason: EgressRefusal, host: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    hosts: frozenset[str]
    host_suffixes: tuple[str, ...]
    port: int = TLS_PORT

    def allows(self, host: str) -> bool:
        return host in self.hosts or any(
            host.endswith(suffix) and len(host) > len(suffix) for suffix in self.host_suffixes
        )


@dataclass(frozen=True, slots=True)
class EgressLimits:
    max_bytes: int
    max_tunnels: int
    head_timeout_seconds: float
    resolve_timeout_seconds: float
    connect_timeout_seconds: float


def embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in NAT64_PREFIX or address in IPV4_COMPATIBLE_PREFIX or address in IPV4_TRANSLATED_PREFIX:
        return ipaddress.IPv4Address(int(address) & IPV4_BITS)
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    return None


def is_public(address: Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = embedded_ipv4(address)
        if embedded is not None:
            return is_public(embedded)
        if address not in GLOBAL_UNICAST:
            return False
    return address.is_global and not address.is_multicast


def public_address(raw: str) -> Address | None:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    return address if is_public(address) else None


async def resolve(host: str, port: int) -> list[str]:
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(answer[4][0]) for answer in answers]


async def connect(address: str, port: int) -> Streams:
    return await asyncio.open_connection(address, port)


@dataclass(frozen=True, slots=True)
class Target:
    host: str
    port: int


def header_value(lines: list[str], name: str) -> str | None:
    values = [
        value.strip()
        for key, separator, value in (line.partition(HEADER_SEPARATOR) for line in lines)
        if separator and key.strip().lower() == name
    ]
    return values[0] if len(values) == 1 else None


def presented_credentials(lines: list[str]) -> str | None:
    authorization = header_value(lines, PROXY_AUTHORIZATION)
    if authorization is None:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != BASIC_SCHEME:
        return None
    try:
        return base64.b64decode(credentials.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None


class EgressSession:
    def __init__(self, token: str, limits: EgressLimits) -> None:
        self._token = token
        self._bytes_left = limits.max_bytes
        self._tunnels_left = limits.max_tunnels
        self.proxy_url = ""
        self.refusals: list[EgressRefusal] = []
        self.hosts: list[str] = []

    def bind(self, port: int) -> None:
        self.proxy_url = f"http://{PROXY_USER}:{self._token}@{LOOPBACK}:{port}"

    def authorised(self, presented: str | None) -> bool:
        expected = f"{PROXY_USER}:{self._token}"
        return presented is not None and hmac.compare_digest(presented.encode(), expected.encode())

    def open_tunnel(self) -> None:
        self._tunnels_left -= 1
        if self._tunnels_left < 0:
            raise RefusedError(EgressRefusal.TUNNELS)

    def spend(self, amount: int) -> None:
        self._bytes_left -= amount
        if self._bytes_left < 0:
            raise RefusedError(EgressRefusal.BYTES)


def parse_head(head: bytes) -> tuple[str, Target | None, list[str]]:
    lines = head.decode(HEAD_ENCODING).split(LINE_END)
    parts = lines[0].split(" ")
    if len(parts) != REQUEST_LINE_PARTS:
        raise RefusedError(EgressRefusal.MALFORMED)
    method, raw_target, _ = parts
    match = TARGET.fullmatch(raw_target.lower())
    target = None if match is None else Target(host=match["host"], port=int(match["port"]))
    return method, target, lines[1:]


@dataclass(frozen=True, slots=True)
class EgressProxy:
    policy: EgressPolicy
    limits: EgressLimits
    metrics: Metrics
    resolver: Resolver = resolve
    connector: Connector = connect

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[EgressSession]:
        session = EgressSession(secrets.token_urlsafe(TOKEN_BYTES), self.limits)
        handlers: set[asyncio.Task[None]] = set()

        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                handlers.add(task)
            try:
                await self._handle(session, reader, writer)
            finally:
                if task is not None:
                    handlers.discard(task)

        server = await asyncio.start_server(accept, LOOPBACK, ANY_PORT, limit=MAX_HEAD_BYTES)
        port = server.sockets[0].getsockname()[1]
        session.bind(port)
        try:
            yield session
        finally:
            server.close()
            for handler in tuple(handlers):
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            await server.wait_closed()

    async def _handle(
        self, session: EgressSession, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            target = await self._admit(session, reader)
            address = await self._pin(target)
            await self._tunnel(session, target, address, (reader, writer))
        except RefusedError as refusal:
            self._refuse(session, refusal, writer)
        except (ConnectionError, OSError):
            logger.info("recording_url_egress_tunnel_dropped")
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _admit(self, session: EgressSession, reader: asyncio.StreamReader) -> Target:
        try:
            async with asyncio.timeout(self.limits.head_timeout_seconds):
                head = await reader.readuntil(HEAD_END)
        except (
            asyncio.LimitOverrunError,
            asyncio.IncompleteReadError,
            TimeoutError,
            ConnectionError,
        ) as error:
            raise RefusedError(EgressRefusal.MALFORMED) from error
        method, target, lines = parse_head(head)
        if method != CONNECT_METHOD:
            raise RefusedError(EgressRefusal.METHOD)
        if not session.authorised(presented_credentials(lines)):
            raise RefusedError(EgressRefusal.CREDENTIALS)
        if target is None:
            raise RefusedError(EgressRefusal.MALFORMED)
        if target.port != self.policy.port:
            raise RefusedError(EgressRefusal.PORT, target.host)
        if not self.policy.allows(target.host):
            raise RefusedError(EgressRefusal.HOST, target.host)
        session.open_tunnel()
        session.hosts.append(target.host)
        return target

    async def _pin(self, target: Target) -> str:
        try:
            async with asyncio.timeout(self.limits.resolve_timeout_seconds):
                answers = await self.resolver(target.host, target.port)
        except (OSError, TimeoutError) as error:
            raise RefusedError(EgressRefusal.RESOLUTION, target.host) from error
        addresses = [public_address(answer) for answer in answers]
        if not addresses:
            raise RefusedError(EgressRefusal.RESOLUTION, target.host)
        checked = [address for address in addresses if address is not None]
        if len(checked) != len(addresses):
            raise RefusedError(EgressRefusal.ADDRESS, target.host)
        return str(checked[0])

    async def _tunnel(self, session: EgressSession, target: Target, address: str, client: Streams) -> None:
        reader, writer = client
        try:
            async with asyncio.timeout(self.limits.connect_timeout_seconds):
                upstream_reader, upstream_writer = await self.connector(address, target.port)
        except (OSError, TimeoutError) as error:
            writer.write(UNREACHABLE)
            raise RefusedError(EgressRefusal.UNREACHABLE, target.host) from error
        try:
            writer.write(ESTABLISHED)
            await writer.drain()
            await self._relay(session, (reader, writer), (upstream_reader, upstream_writer))
        finally:
            upstream_writer.close()
            with suppress(ConnectionError, OSError):
                await upstream_writer.wait_closed()

    async def _relay(self, session: EgressSession, client: Streams, upstream: Streams) -> None:
        copies = {
            asyncio.ensure_future(self._copy(session, client[0], upstream[1])),
            asyncio.ensure_future(self._copy(session, upstream[0], client[1])),
        }
        try:
            done, _ = await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for copy in copies:
                copy.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
        for copy in done:
            failure = copy.exception()
            if isinstance(failure, RefusedError):
                raise failure

    @staticmethod
    async def _copy(session: EgressSession, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        with suppress(ConnectionError, OSError):
            while chunk := await source.read(COPY_CHUNK_BYTES):
                session.spend(len(chunk))
                sink.write(chunk)
                await sink.drain()

    def _refuse(self, session: EgressSession, refusal: RefusedError, writer: asyncio.StreamWriter) -> None:
        session.refusals.append(refusal.reason)
        self.metrics.recording_url_egress_refusals.labels(reason=refusal.reason).inc()
        logger.warning("recording_url_egress_refused", extra={"reason": refusal.reason, "host": refusal.host})
        if refusal.reason not in {EgressRefusal.UNREACHABLE, EgressRefusal.BYTES}:
            writer.write(REFUSED)
