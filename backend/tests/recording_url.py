import asyncio
import base64
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from deckly.infrastructure.recording_url.egress import EgressLimits, EgressPolicy, Streams

ALLOWED_HOST = "www.youtube.com"
MEDIA_HOST = "rr1---sn-test.googlevideo.com"
PUBLIC_ADDRESS = "142.250.74.110"
TEST_POLICY = EgressPolicy(hosts=frozenset({ALLOWED_HOST}), host_suffixes=(".googlevideo.com",))
TEST_LIMITS = EgressLimits(
    max_bytes=64 * 1024,
    max_tunnels=8,
    head_timeout_seconds=0.5,
    resolve_timeout_seconds=0.5,
    connect_timeout_seconds=0.5,
)
STATUS_LINE_END = b"\r\n"
RESPONSE_HEAD_END = b"\r\n\r\n"


@dataclass
class Listener:
    port: int = 0
    connections: int = 0
    received: bytearray = field(default_factory=bytearray)
    reply: bytes = b""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        if self.reply:
            writer.write(self.reply)
            await writer.drain()
        try:
            while chunk := await reader.read(4096):
                self.received += chunk
                writer.write(chunk)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()


@asynccontextmanager
async def listening(reply: bytes = b"") -> AsyncIterator[Listener]:
    listener = Listener(reply=reply)
    server = await asyncio.start_server(listener.handle, "127.0.0.1", 0)
    listener.port = server.sockets[0].getsockname()[1]
    try:
        yield listener
    finally:
        server.close()
        await server.wait_closed()


@dataclass
class ScriptedResolver:
    answers: dict[str, list[list[str]]]
    calls: list[str] = field(default_factory=list)

    async def __call__(self, host: str, port: int) -> list[str]:
        del port
        self.calls.append(host)
        scripted = self.answers.get(host)
        if not scripted:
            raise OSError(host)
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]


def resolving(host: str, *answers: Iterable[str]) -> ScriptedResolver:
    return ScriptedResolver({host: [list(answer) for answer in answers]})


@dataclass
class LoopbackConnector:
    port: int
    dialled: list[str] = field(default_factory=list)

    async def __call__(self, address: str, port: int) -> Streams:
        del port
        self.dialled.append(address)
        return await asyncio.open_connection("127.0.0.1", self.port)


@dataclass(frozen=True, slots=True)
class ProxyAddress:
    host: str
    port: int
    credentials: str


def proxy_address(proxy_url: str) -> ProxyAddress:
    parts = urlsplit(proxy_url)
    assert parts.hostname is not None
    assert parts.port is not None
    credentials = base64.b64encode(f"{parts.username}:{parts.password}".encode()).decode()
    return ProxyAddress(host=parts.hostname, port=parts.port, credentials=credentials)


async def send_head(proxy_url: str, head: bytes) -> tuple[bytes, Streams]:
    address = proxy_address(proxy_url)
    reader, writer = await asyncio.open_connection(address.host, address.port)
    writer.write(head)
    await writer.drain()
    try:
        response = await reader.readuntil(RESPONSE_HEAD_END)
    except asyncio.IncompleteReadError as cut:
        response = cut.partial
    return response.split(STATUS_LINE_END)[0], (reader, writer)


def connect_head(target: str, proxy_url: str | None = None, *, method: str = "CONNECT") -> bytes:
    lines = [f"{method} {target} HTTP/1.1", f"Host: {target}"]
    if proxy_url is not None:
        lines.append(f"Proxy-Authorization: Basic {proxy_address(proxy_url).credentials}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def tunnel(proxy_url: str, target: str) -> tuple[bytes, Streams]:
    return await send_head(proxy_url, connect_head(target, proxy_url))


async def close(streams: Streams) -> None:
    _, writer = streams
    writer.close()
    with suppress(ConnectionError):
        await writer.wait_closed()
