import asyncio
import logging
import ssl
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import Protocol
from urllib.parse import quote, urljoin

import httpx2

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import DocumentUpload, InspectedUpload
from deckly.domain.article_url import ArticleUrl, parse_redirect_url
from deckly.domain.document import DocumentFormat, clean_filename
from deckly.domain.exceptions import (
    ArticleBlockedByRobotsError,
    ArticleHasNoTextError,
    ArticleUnavailableError,
    InvalidDocumentError,
    UnsupportedArticleUrlError,
    UnsupportedDocumentError,
)
from deckly.domain.text import strip_unstorable, visible_length
from deckly.infrastructure.article_url.extracted import ExtractedArticle
from deckly.infrastructure.article_url.extraction import (
    ExtractorInvocation,
    SubprocessArticleExtractor,
    require_extractor,
)
from deckly.infrastructure.article_url.robots import (
    ALLOW_ALL,
    MAX_ROBOTS_BYTES,
    MAX_ROBOTS_REDIRECTS,
    ROBOTS_PATH,
    ROBOTS_PRODUCT,
    RobotsRules,
    RobotsUnreachableError,
    is_success,
    robots_rules,
)
from deckly.infrastructure.observability.metrics import Metrics
from deckly.infrastructure.recording_url.egress import (
    EgressLimits,
    EgressProxy,
    EgressRefusal,
    EgressSession,
    EgressSubject,
    PublicHostPolicy,
)
from deckly.infrastructure.search.cleaning import cut_at_word
from deckly.infrastructure.search.parser import MIN_VISIBLE_CHARACTERS

logger = logging.getLogger(__name__)

MEBIBYTE = 1024 * 1024
MAX_UTF8_BYTES_PER_CHARACTER = 4
FETCH_RETRY_AFTER_SECONDS = 60
MAX_PAGE_BYTES = 2 * MEBIBYTE
MAX_REDIRECTS = 5
MAX_DRAINED_BODY_BYTES = 64 * 1024
MAX_TUNNELS_PER_FETCH = 24
EGRESS_ALLOWANCE_BYTES = 8 * MEBIBYTE
HEAD_TIMEOUT_SECONDS = 10.0
RESOLVE_TIMEOUT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 10.0
EXTRACTION_DEADLINE_SECONDS = 15.0
MAX_TITLE_CHARACTERS = 200
PATH_SEPARATORS = ("/", "\\")
TITLE_SEPARATOR_REPLACEMENT = "-"
MAX_LOGGED_ERROR_CHARACTERS = 300
MAX_ERROR_CHAIN = 8
GZIP_WINDOW_BITS = zlib.MAX_WBITS | 16
CONTENT_TYPE = "content-type"
CONTENT_ENCODING = "content-encoding"
LOCATION = "location"
CHARSET_PARAMETER = "charset"
PARAMETER_SEPARATOR = ";"
VALUE_SEPARATOR = "="
QUOTES = "\"'"
LOCATION_SAFE_CHARACTERS = "!#$%&'()*+,-./:;=?@[]^_`{|}~"
USER_AGENT = f"Mozilla/5.0 (compatible; {ROBOTS_PRODUCT}/1.0; user-requested fetch)"
REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
ROBOTS_HEADERS = {"Accept": "text/plain"}
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
IDENTITY_ENCODINGS = frozenset({"", "identity"})
GZIP_ENCODINGS = frozenset({"gzip", "x-gzip"})
REDIRECT_STATUSES = frozenset(
    {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
)
RETRYABLE_STATUSES = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})
REACHABILITY_REFUSALS = frozenset(
    {EgressRefusal.HOST, EgressRefusal.PORT, EgressRefusal.RESOLUTION, EgressRefusal.ADDRESS}
)
UNREACHABLE_MESSAGE = "the page could not be reached"
NO_SECURE_ANSWER_MESSAGE = (
    "the site did not answer over https, and plain http links are fetched over https only"
)
NO_TEXT_MESSAGE = (
    "the page has no readable article text in its HTML; pages that build their content with "
    "JavaScript in the browser are not supported"
)


class ArticleFetchOutcome(StrEnum):
    FETCHED = "fetched"
    UNAVAILABLE = "unavailable"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"
    ROBOTS_UNREACHABLE = "robots_unreachable"
    NOT_HTML = "not_html"
    NO_TEXT = "no_text"
    EGRESS_REFUSED = "egress_refused"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class ArticleTruncation(StrEnum):
    BYTES = "bytes"
    MARKUP = "markup"
    CHARACTERS = "characters"


class ArticleEgressRefusedError(UpstreamUnavailableError):
    pass


OUTCOMES: Mapping[type[Exception], ArticleFetchOutcome] = {
    ArticleUnavailableError: ArticleFetchOutcome.UNAVAILABLE,
    ArticleBlockedByRobotsError: ArticleFetchOutcome.BLOCKED_BY_ROBOTS,
    RobotsUnreachableError: ArticleFetchOutcome.ROBOTS_UNREACHABLE,
    UnsupportedDocumentError: ArticleFetchOutcome.NOT_HTML,
    ArticleHasNoTextError: ArticleFetchOutcome.NO_TEXT,
    ArticleEgressRefusedError: ArticleFetchOutcome.EGRESS_REFUSED,
}


def outcome_of(error: Exception) -> ArticleFetchOutcome:
    for kind in type(error).__mro__:
        outcome = OUTCOMES.get(kind)
        if outcome is not None:
            return outcome
    return ArticleFetchOutcome.FAILED


def unavailable(detail: str) -> ArticleUnavailableError:
    return ArticleUnavailableError(f"the page cannot be fetched: {detail}")


def upstream_failure(detail: str) -> UpstreamUnavailableError:
    error = UpstreamUnavailableError(FETCH_RETRY_AFTER_SECONDS)
    error.add_note(detail)
    return error


def is_certificate_failure(error: BaseException) -> bool:
    current: BaseException | None = error
    for _ in range(MAX_ERROR_CHAIN):
        if current is None:
            return False
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(slots=True)
class FetchProgress:
    answered: bool = False


@dataclass(frozen=True, slots=True)
class FetchAttempt:
    article: ArticleUrl
    session: EgressSession
    progress: FetchProgress = field(default_factory=FetchProgress)

    @property
    def awaits_first_secure_answer(self) -> bool:
        return self.article.upgraded and not self.progress.answered


def transport_failure(error: httpx2.HTTPError, attempt: FetchAttempt) -> Exception:
    refusals = set(attempt.session.refusals)
    failure: Exception
    if refusals & REACHABILITY_REFUSALS:
        failure = ArticleUnavailableError(UNREACHABLE_MESSAGE)
    elif refusals - {EgressRefusal.UNREACHABLE}:
        failure = ArticleEgressRefusedError(FETCH_RETRY_AFTER_SECONDS)
    elif attempt.awaits_first_secure_answer:
        failure = unavailable(NO_SECURE_ANSWER_MESSAGE)
    elif not isinstance(error, httpx2.TimeoutException) and is_certificate_failure(error):
        failure = unavailable("the site's secure connection could not be verified")
    else:
        failure = UpstreamUnavailableError(FETCH_RETRY_AFTER_SECONDS)
    failure.add_note(f"{type(error).__name__}; egress refusals: {sorted(refusals)}")
    return failure


class BodyDecoder(Protocol):
    def feed(self, chunk: bytes, limit: int) -> bytes: ...


class IdentityBody:
    def feed(self, chunk: bytes, limit: int) -> bytes:
        return chunk[:limit]


class GzipBody:
    def __init__(self) -> None:
        self._inflater = zlib.decompressobj(GZIP_WINDOW_BITS)

    def feed(self, chunk: bytes, limit: int) -> bytes:
        try:
            return self._inflater.decompress(chunk, limit)
        except zlib.error as error:
            message = "its compressed body is damaged"
            raise unavailable(message) from error


def body_decoder(encoding: str | None) -> BodyDecoder:
    value = (encoding or "").strip().lower()
    if value in IDENTITY_ENCODINGS:
        return IdentityBody()
    if value in GZIP_ENCODINGS:
        return GzipBody()
    message = "it is sent with a content encoding other than gzip"
    raise unavailable(message)


@dataclass(frozen=True, slots=True)
class CappedBody:
    content: bytes
    truncated: bool


async def read_capped(response: httpx2.Response, max_bytes: int) -> CappedBody:
    decoder = body_decoder(response.headers.get(CONTENT_ENCODING))
    kept = bytearray()
    async for chunk in response.aiter_raw():
        kept += decoder.feed(chunk, max_bytes + 1 - len(kept))
        if len(kept) > max_bytes:
            return CappedBody(content=bytes(kept[:max_bytes]), truncated=True)
    return CappedBody(content=bytes(kept), truncated=False)


async def drain(response: httpx2.Response) -> None:
    drained = 0
    async for chunk in response.aiter_raw():
        drained += len(chunk)
        if drained > MAX_DRAINED_BODY_BYTES:
            return


def media_type(response: httpx2.Response) -> tuple[str, str | None]:
    essence, *parameters = response.headers.get(CONTENT_TYPE, "").split(PARAMETER_SEPARATOR)
    charset = None
    for parameter in parameters:
        key, _, value = parameter.partition(VALUE_SEPARATOR)
        if key.strip().lower() == CHARSET_PARAMETER:
            charset = value.strip().strip(QUOTES) or None
    return essence.strip().lower(), charset


def check_page_status(status: int) -> None:
    if status == HTTPStatus.OK:
        return
    if status in RETRYABLE_STATUSES or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        message = f"the site answered HTTP {status}"
        raise upstream_failure(message)
    message = f"the site answered HTTP {status}"
    raise unavailable(message)


def redirect_target(response: httpx2.Response, current: ArticleUrl) -> ArticleUrl | None:
    location = response.headers.get(LOCATION)
    if location is None:
        return None
    try:
        absolute = urljoin(current.url, quote(location.strip(), safe=LOCATION_SAFE_CHARACTERS))
        return parse_redirect_url(absolute)
    except (UnsupportedArticleUrlError, ValueError):
        return None


def filename_for(title: str | None, url: ArticleUrl) -> str:
    stem = title or ""
    for separator in PATH_SEPARATORS:
        stem = stem.replace(separator, TITLE_SEPARATOR_REPLACEMENT)
    try:
        return clean_filename(stem[:MAX_TITLE_CHARACTERS])
    except InvalidDocumentError:
        return url.host


@dataclass(frozen=True, slots=True)
class ArticleText:
    upload: DocumentUpload
    truncated: bool


def article_text(extracted: ExtractedArticle, url: ArticleUrl, max_characters: int) -> ArticleText:
    raw = strip_unstorable(extracted.text or "")
    text = cut_at_word(raw, max_characters)
    if visible_length(text) < MIN_VISIBLE_CHARACTERS:
        raise ArticleHasNoTextError(NO_TEXT_MESSAGE)
    upload = DocumentUpload(
        filename=filename_for(extracted.title, url), content=text.encode(), source_url=url.url
    )
    return ArticleText(upload=upload, truncated=len(text) < len(raw))


class ArticleTextInspector:
    async def identify(self, content: bytes) -> InspectedUpload:
        try:
            content.decode()
        except UnicodeDecodeError as error:
            message = "the fetched page is not UTF-8 text"
            raise UnsupportedDocumentError(message) from error
        return InspectedUpload(document_format=DocumentFormat.TEXT)


class ArticleExtractor(Protocol):
    async def extract(self, content: bytes, charset: str | None) -> ExtractedArticle: ...


@dataclass(frozen=True, slots=True)
class ArticleFetchLimits:
    max_characters: int
    timeout_seconds: float
    max_page_bytes: int = MAX_PAGE_BYTES
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: ArticleUrl
    body: CappedBody
    charset: str | None


@dataclass(frozen=True, slots=True)
class HttpArticleFetcher:
    proxy: EgressProxy
    extractor: ArticleExtractor
    limits: ArticleFetchLimits
    metrics: Metrics
    verify: ssl.SSLContext | bool = True

    async def fetch(self, article: ArticleUrl) -> DocumentUpload:
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                upload = await self._fetch(article)
        except TimeoutError as error:
            self._record(ArticleFetchOutcome.TIMED_OUT, article)
            message = f"fetching the page took longer than {self.limits.timeout_seconds} seconds"
            raise upstream_failure(message) from error
        except Exception as error:
            self._record(outcome_of(error), article, error)
            raise
        self._record(ArticleFetchOutcome.FETCHED, article)
        return upload

    async def _fetch(self, article: ArticleUrl) -> DocumentUpload:
        async with self.proxy.serve() as session:
            attempt = FetchAttempt(article=article, session=session)
            try:
                async with self._client(session) as client:
                    page = await self._page(client, attempt)
            except httpx2.HTTPError as error:
                raise transport_failure(error, attempt) from error
            logger.info("article_url_hosts", extra={"hosts": sorted(set(session.hosts))})
        extracted = await self.extractor.extract(page.body.content, page.charset)
        text = article_text(extracted, page.url, self.limits.max_characters)
        self._count_truncations(
            {
                ArticleTruncation.BYTES: page.body.truncated,
                ArticleTruncation.MARKUP: extracted.markup_truncated,
                ArticleTruncation.CHARACTERS: text.truncated,
            }
        )
        return text.upload

    def _client(self, session: EgressSession) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            proxy=session.proxy_url,
            verify=self.verify,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx2.Timeout(self.limits.request_timeout_seconds),
            headers=REQUEST_HEADERS,
        )

    async def _page(self, client: httpx2.AsyncClient, attempt: FetchAttempt) -> FetchedPage:
        rules: dict[str, RobotsRules] = {}
        url = attempt.article
        for _ in range(MAX_REDIRECTS + 1):
            if url.host not in rules:
                rules[url.host] = await self._robots(client, url, attempt.progress)
            if not rules[url.host].allows(url.url):
                message = "the site's robots.txt asks automated clients not to fetch this page"
                raise ArticleBlockedByRobotsError(message)
            async with client.stream("GET", url.url, headers=PAGE_HEADERS) as response:
                attempt.progress.answered = True
                if response.status_code not in REDIRECT_STATUSES:
                    return await self._read_page(response, url)
                target = redirect_target(response, url)
                await drain(response)
            if target is None:
                message = (
                    "it redirects to an address that is not fetched: plain http, an IP address, "
                    "a local name or a non-standard port"
                )
                raise unavailable(message)
            url = target
        message = f"it redirects more than {MAX_REDIRECTS} times"
        raise unavailable(message)

    async def _read_page(self, response: httpx2.Response, url: ArticleUrl) -> FetchedPage:
        check_page_status(response.status_code)
        essence, charset = media_type(response)
        if essence not in HTML_MEDIA_TYPES:
            message = f"the link is not a web page: the site sent {essence or 'no content type'}"
            raise UnsupportedDocumentError(message)
        body = await read_capped(response, self.limits.max_page_bytes)
        return FetchedPage(url=url, body=body, charset=charset)

    @staticmethod
    async def _robots(client: httpx2.AsyncClient, page: ArticleUrl, progress: FetchProgress) -> RobotsRules:
        url = ArticleUrl(host=page.host, target=ROBOTS_PATH)
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            async with client.stream("GET", url.url, headers=ROBOTS_HEADERS) as response:
                progress.answered = True
                status = response.status_code
                if is_success(status):
                    body = await read_capped(response, MAX_ROBOTS_BYTES)
                    return robots_rules(status, body.content)
                await drain(response)
                if status not in REDIRECT_STATUSES:
                    return robots_rules(status, b"")
                target = redirect_target(response, url)
            if target is None:
                return ALLOW_ALL
            url = target
        return ALLOW_ALL

    def _count_truncations(self, truncations: Mapping[ArticleTruncation, bool]) -> None:
        for limit, truncated in truncations.items():
            if truncated:
                self.metrics.article_url_truncations.labels(limit=limit).inc()
                logger.info("article_url_truncated", extra={"limit": limit})

    def _record(
        self, outcome: ArticleFetchOutcome, article: ArticleUrl, error: Exception | None = None
    ) -> None:
        self.metrics.article_url_fetches.labels(outcome=outcome).inc()
        context = {"outcome": outcome, "host": article.host}
        if error is None:
            logger.info("article_url_fetched", extra=context)
            return
        detail = " ".join(getattr(error, "__notes__", ())) or str(error)
        logger.warning(
            "article_url_fetch_failed", extra={**context, "detail": detail[:MAX_LOGGED_ERROR_CHARACTERS]}
        )


def egress_limits(max_page_bytes: int) -> EgressLimits:
    return EgressLimits(
        max_bytes=max_page_bytes + EGRESS_ALLOWANCE_BYTES,
        max_tunnels=MAX_TUNNELS_PER_FETCH,
        head_timeout_seconds=HEAD_TIMEOUT_SECONDS,
        resolve_timeout_seconds=RESOLVE_TIMEOUT_SECONDS,
        connect_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
    )


def build_article_fetcher(limits: ArticleFetchLimits, metrics: Metrics) -> HttpArticleFetcher:
    require_extractor()
    return HttpArticleFetcher(
        proxy=EgressProxy(
            PublicHostPolicy(),
            egress_limits(limits.max_page_bytes),
            metrics,
            subject=EgressSubject.ARTICLE_URL,
        ),
        extractor=SubprocessArticleExtractor(
            ExtractorInvocation(max_characters=limits.max_characters + 1),
            deadline_seconds=EXTRACTION_DEADLINE_SECONDS,
        ),
        limits=limits,
        metrics=metrics,
    )
