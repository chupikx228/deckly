import asyncio
import shutil
import ssl
import threading
from asyncio.subprocess import PIPE
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

from deckly.infrastructure.article_url.extracted import ExtractedArticle
from deckly.infrastructure.article_url.extractor import extract
from deckly.infrastructure.article_url.fetcher import ArticleFetchLimits, HttpArticleFetcher, egress_limits
from deckly.infrastructure.recording_url.egress import EgressProxy, EgressSubject, PublicHostPolicy, Streams
from tests.fakes import fresh_metrics
from tests.recording_url import PUBLIC_ADDRESS, ScriptedResolver

ARTICLE_HOST = "article.example.com"
OTHER_HOST = "other.example.org"
ROBOTS_HOST = "robots.example.org"
UNCERTIFIED_HOST = "uncertified.example.net"
METADATA_HOST = "metadata.example.com"
SECOND_PUBLIC_ADDRESS = "151.101.1.69"
METADATA_ADDRESS = "169.254.169.254"
CERTIFIED_NAMES = (ARTICLE_HOST, f"*.{ARTICLE_HOST.partition('.')[2]}", OTHER_HOST, ROBOTS_HOST)
HTML = "text/html; charset=utf-8"
TEST_MAX_CHARACTERS = 20_000
TEST_LIMITS = ArticleFetchLimits(max_characters=TEST_MAX_CHARACTERS, timeout_seconds=10)
HEAD_END = b"\r\n\r\n"
LINE_END = "\r\n"
HOST_HEADER = "host"
NAVIGATION = (
    "<header><nav><a href='/'>Home</a> <a href='/news'>News</a> "
    "<a href='/login'>Sign in to your account</a></nav></header>"
)
COOKIE_BANNER = (
    "<div class='cookie-banner'>We use cookies to improve your experience. Accept all cookies?</div>"
)
FOOTER = (
    "<footer><p>Copyright 2026 Example Media. All rights reserved. "
    "Privacy policy and cookie settings.</p></footer>"
)
ARTICLE_TITLE = "Spaced repetition explained"
ARTICLE_PARAGRAPHS = (
    (
        "Spaced repetition is a learning technique in which reviews of a fact are spread out over "
        "increasing intervals of time."
    ),
    (
        "The method exploits the spacing effect, first described by Hermann Ebbinghaus in 1885, who "
        "measured how quickly memories fade."
    ),
    (
        "Modern flashcard programs schedule each card individually, showing difficult cards more often "
        "and easy cards less often."
    ),
    (
        "Algorithms such as SM-2 and FSRS estimate the stability of a memory and pick the next review "
        "date accordingly."
    ),
)
SINGLE_PAGE_APP = (
    "<!doctype html><html><head><title>App</title><script src='/static/js/main.js'></script></head>"
    "<body><noscript>You need to enable JavaScript to run this app.</noscript>"
    "<div id='root'></div></body></html>"
)
RUSSIAN_TITLE = "Интервальные повторения"
RUSSIAN_PARAGRAPHS = (
    (
        "Интервальные повторения — техника удержания в памяти, заключающаяся в повторении "
        "запомненного материала по определённым правилам."
    ),
    "Чем лучше материал запомнен, тем реже нужно повторять; сложные карточки показываются чаще.",
    "Метод основан на эффекте интервалов, описанном Германом Эббингаузом в конце девятнадцатого века.",
)


def article_page(
    title: str | None = ARTICLE_TITLE, paragraphs: Iterable[str] = ARTICLE_PARAGRAPHS, padding: str = ""
) -> str:
    head = "" if title is None else f"<title>{title}</title>"
    heading = "" if title is None else f"<h1>{title}</h1>"
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>{head}</head><body>{NAVIGATION}{COOKIE_BANNER}"
        f"<main><article>{heading}{body}</article></main>{padding}{FOOTER}</body></html>"
    )


def article_bytes(**changes: str) -> bytes:
    return article_page(**changes).encode()


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    authority: Path
    certificate: Path
    key: Path

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.authority))

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.certificate), str(self.key))
        return context


async def openssl(*arguments: str) -> None:
    binary = shutil.which("openssl")
    assert binary is not None, "the article fetch tests need the openssl command to issue a test certificate"
    process = await asyncio.create_subprocess_exec(binary, *arguments, stdout=PIPE, stderr=PIPE)
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")


async def issue_tls_material(directory: Path) -> TlsMaterial:
    authority, authority_key = directory / "ca.pem", directory / "ca.key"
    certificate, key, request = directory / "server.pem", directory / "server.key", directory / "server.csr"
    extensions = directory / "server.ext"
    names = ",".join(f"DNS:{name}" for name in CERTIFIED_NAMES)
    extensions.write_text(f"subjectAltName={names}\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n")
    await openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-keyout",
        str(authority_key),
        "-out",
        str(authority),
        "-subj",
        "/CN=Deckly test authority",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    await openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(request),
        "-subj",
        f"/CN={ARTICLE_HOST}",
    )
    await openssl(
        "x509",
        "-req",
        "-in",
        str(request),
        "-CA",
        str(authority),
        "-CAkey",
        str(authority_key),
        "-CAcreateserial",
        "-out",
        str(certificate),
        "-days",
        "2",
        "-extfile",
        str(extensions),
    )
    return TlsMaterial(authority=authority, certificate=certificate, key=key)


@dataclass
class Reply:
    status: int = HTTPStatus.OK
    headers: tuple[tuple[str, str], ...] = (("Content-Type", HTML),)
    body: bytes = b""
    endless: bytes | None = None
    declared_length: int | None = None
    close: bool = False
    stall_seconds: float = 0.0
    hang_up: bool = False


def page(body: bytes | str, content_type: str = HTML, *, close: bool = False) -> Reply:
    content = body.encode() if isinstance(body, str) else body
    return Reply(headers=(("Content-Type", content_type),), body=content, close=close)


def redirect(location: str, status: int = HTTPStatus.FOUND) -> Reply:
    return Reply(status=status, headers=(("Location", location),))


NOT_FOUND = Reply(status=HTTPStatus.NOT_FOUND, headers=(("Content-Type", "text/plain"),), body=b"not found")


def chunked(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def parse_request(head: bytes) -> tuple[str, str]:
    lines = head.decode("latin-1").split(LINE_END)
    _, path, _ = lines[0].split(" ", 2)
    host = ""
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == HOST_HEADER:
            host = value.strip().partition(":")[0]
    return host, path


@dataclass
class Site:
    replies: dict[tuple[str, str], Reply] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    heads: list[bytes] = field(default_factory=list)
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    connections: int = 0
    sent_bytes: int = 0
    port: int = 0

    def serve(self, host: str, path: str, reply: Reply) -> None:
        self.replies[host, path] = reply

    def paths(self, host: str = ARTICLE_HOST) -> list[str]:
        return [path for requested, path in self.requests if requested == host]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.add(writer)
        try:
            while True:
                head = await reader.readuntil(HEAD_END)
                host, path = parse_request(head)
                self.requests.append((host, path))
                self.heads.append(head)
                reply = self.replies.get((host, path), NOT_FOUND)
                if reply.hang_up:
                    return
                if reply.stall_seconds:
                    await asyncio.sleep(reply.stall_seconds)
                await self._write(writer, reply)
                if reply.close or reply.endless is not None:
                    return
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            return
        finally:
            self.writers.discard(writer)
            writer.close()
            with suppress(ConnectionError, ssl.SSLError, OSError):
                await writer.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, reply: Reply) -> None:
        reason = HTTPStatus(reply.status).phrase
        lines = [f"HTTP/1.1 {reply.status} {reason}", *(f"{name}: {value}" for name, value in reply.headers)]
        if reply.declared_length is not None:
            lines.append(f"Content-Length: {reply.declared_length}")
        elif reply.endless is not None:
            lines.append("Transfer-Encoding: chunked")
        else:
            lines.append(f"Content-Length: {len(reply.body)}")
        if reply.close:
            lines.append("Connection: close")
        writer.write((LINE_END.join(lines) + LINE_END + LINE_END).encode())
        if reply.endless is None:
            writer.write(reply.body)
            self.sent_bytes += len(reply.body)
            await writer.drain()
            return
        framed = reply.declared_length is None
        writer.write(chunked(reply.body) if framed else reply.body)
        while True:
            writer.write(chunked(reply.endless) if framed else reply.endless)
            self.sent_bytes += len(reply.endless)
            await writer.drain()


def ignore_loop_errors(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
    del loop, context


@asynccontextmanager
async def serving(tls: TlsMaterial, site: Site | None = None) -> AsyncIterator[Site]:
    served = site or Site()
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(ignore_loop_errors)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def start() -> asyncio.Server:
        return await asyncio.start_server(served.handle, "127.0.0.1", 0, ssl=tls.server_context())

    async def stop(server: asyncio.Server) -> None:
        server.close()
        for writer in tuple(served.writers):
            writer.transport.abort()
        await server.wait_closed()

    server = await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(start(), loop))
    served.port = server.sockets[0].getsockname()[1]
    try:
        yield served
    finally:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(stop(server), loop))
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.to_thread(thread.join)
        loop.close()


@dataclass
class RoutingConnector:
    routes: dict[str, int]
    dialled: list[str] = field(default_factory=list)

    async def __call__(self, address: str, port: int) -> Streams:
        del port
        self.dialled.append(address)
        target = self.routes.get(address)
        if target is None:
            raise OSError(address)
        return await asyncio.open_connection("127.0.0.1", target)


def resolving_hosts(answers: dict[str, list[list[str]]]) -> ScriptedResolver:
    return ScriptedResolver(
        {host: [list(answer) for answer in scripted] for host, scripted in answers.items()}
    )


def public_dns(*hosts: str) -> ScriptedResolver:
    return resolving_hosts({host: [[PUBLIC_ADDRESS]] for host in hosts})


@dataclass(frozen=True, slots=True)
class InProcessExtractor:
    max_characters: int = TEST_MAX_CHARACTERS + 1

    async def extract(self, content: bytes, charset: str | None) -> ExtractedArticle:
        return await asyncio.to_thread(extract, content, charset, self.max_characters)


def article_fetcher(
    tls: TlsMaterial,
    resolver: ScriptedResolver,
    connector: RoutingConnector,
    limits: ArticleFetchLimits = TEST_LIMITS,
) -> HttpArticleFetcher:
    metrics = fresh_metrics()
    return HttpArticleFetcher(
        proxy=EgressProxy(
            PublicHostPolicy(),
            egress_limits(limits.max_page_bytes),
            metrics,
            resolver=resolver,
            connector=connector,
            subject=EgressSubject.ARTICLE_URL,
        ),
        extractor=InProcessExtractor(limits.max_characters + 1),
        limits=limits,
        metrics=metrics,
        verify=tls.client_context(),
    )
