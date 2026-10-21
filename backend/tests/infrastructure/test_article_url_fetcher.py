import gzip
import tracemalloc
from dataclasses import replace
from http import HTTPStatus

import httpx2
import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.article_url import ArticleUrl, parse_article_url
from deckly.domain.exceptions import (
    ArticleBlockedByRobotsError,
    ArticleHasNoTextError,
    ArticleUnavailableError,
    UnsupportedDocumentError,
)
from deckly.infrastructure.article_url.extracted import MAX_PAGE_NODES, ExtractedArticle
from deckly.infrastructure.article_url.fetcher import (
    FETCH_RETRY_AFTER_SECONDS,
    MAX_LOGGED_ERROR_CHARACTERS,
    MAX_PAGE_BYTES,
    MAX_REDIRECTS,
    NO_SECURE_ANSWER_MESSAGE,
    UNREACHABLE_MESSAGE,
    USER_AGENT,
    ArticleEgressRefusedError,
    ArticleFetchOutcome,
    ArticleTruncation,
    FetchAttempt,
    HttpArticleFetcher,
    article_text,
    transport_failure,
)
from deckly.infrastructure.article_url.robots import (
    MAX_ROBOTS_BYTES,
    MAX_ROBOTS_REDIRECTS,
    RobotsUnreachableError,
)
from deckly.infrastructure.recording_url.egress import EgressLimits, EgressRefusal, EgressSession
from tests.article_url import (
    ARTICLE_HOST,
    ARTICLE_PARAGRAPHS,
    ARTICLE_TITLE,
    COOKIE_BANNER,
    FOOTER,
    METADATA_ADDRESS,
    METADATA_HOST,
    NAVIGATION,
    OTHER_HOST,
    ROBOTS_HOST,
    RUSSIAN_PARAGRAPHS,
    RUSSIAN_TITLE,
    SECOND_PUBLIC_ADDRESS,
    SINGLE_PAGE_APP,
    TEST_LIMITS,
    UNCERTIFIED_HOST,
    Reply,
    RoutingConnector,
    Site,
    TlsMaterial,
    article_bytes,
    article_fetcher,
    article_page,
    page,
    public_dns,
    redirect,
    resolving_hosts,
    serving,
)
from tests.logs import captured_json_logs
from tests.recording_url import PUBLIC_ADDRESS, listening

pytestmark = pytest.mark.anyio

ARTICLE_PATH = "/science/spaced-repetition"
ARTICLE_URL = f"https://{ARTICLE_HOST}{ARTICLE_PATH}"
ROBOTS = "/robots.txt"
METADATA_PATH = "/latest/meta-data/iam/security-credentials/"
DISALLOW_SCIENCE = b"User-agent: *\nDisallow: /science/\n"
FILLER = b"<p>filler paragraph that repeats</p>"
HTML_TYPE = "text/html"


def article(url: str = ARTICLE_URL) -> ArticleUrl:
    return parse_article_url(url)


def boilerplate_sentences() -> list[str]:
    return ["Sign in to your account", "We use cookies", "All rights reserved"]


def metric(fetcher: HttpArticleFetcher, name: str, labels: dict[str, str]) -> float:
    value = fetcher.metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def fetches(fetcher: HttpArticleFetcher, outcome: ArticleFetchOutcome) -> float:
    return metric(fetcher, "deckly_article_url_fetches_total", {"outcome": outcome})


def refusals(fetcher: HttpArticleFetcher, reason: EgressRefusal) -> float:
    return metric(fetcher, "deckly_article_url_egress_refusals_total", {"reason": reason})


def truncations(fetcher: HttpArticleFetcher, limit: ArticleTruncation) -> float:
    return metric(fetcher, "deckly_article_url_truncations_total", {"limit": limit})


def serve_article(site: Site, body: bytes | None = None, host: str = ARTICLE_HOST) -> None:
    site.serve(host, ARTICLE_PATH, page(article_bytes() if body is None else body))


def connector_to(site: Site, **extra: int) -> RoutingConnector:
    return RoutingConnector({PUBLIC_ADDRESS: site.port, SECOND_PUBLIC_ADDRESS: site.port, **extra})


async def test_article_is_fetched_after_robots_and_stored_as_its_readable_text(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        serve_article(site)
        connector = connector_to(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector)

        upload = await fetcher.fetch(article())

    text = upload.content.decode()
    assert upload.filename == ARTICLE_TITLE
    assert upload.source_url == ARTICLE_URL
    assert all(paragraph in text for paragraph in ARTICLE_PARAGRAPHS)
    assert not [sentence for sentence in boilerplate_sentences() if sentence in text]
    assert site.requests == [(ARTICLE_HOST, ROBOTS), (ARTICLE_HOST, ARTICLE_PATH)]
    assert all(f"User-Agent: {USER_AGENT}".encode() in head for head in site.heads)
    assert set(connector.dialled) == {PUBLIC_ADDRESS}
    assert fetches(fetcher, ArticleFetchOutcome.FETCHED) == 1


async def test_redirect_to_another_host_is_followed_with_its_own_checks_and_the_final_url_is_cited(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(
            ARTICLE_HOST, "/s/42", redirect(f"https://{OTHER_HOST}/articles/42", HTTPStatus.MOVED_PERMANENTLY)
        )
        site.serve(OTHER_HOST, "/articles/42", page(article_bytes()))
        resolver = resolving_hosts({ARTICLE_HOST: [[PUBLIC_ADDRESS]], OTHER_HOST: [[SECOND_PUBLIC_ADDRESS]]})
        connector = connector_to(site)
        fetcher = article_fetcher(tls_material, resolver, connector)

        upload = await fetcher.fetch(article(f"https://{ARTICLE_HOST}/s/42"))

    assert upload.source_url == f"https://{OTHER_HOST}/articles/42"
    assert site.requests == [
        (ARTICLE_HOST, ROBOTS),
        (ARTICLE_HOST, "/s/42"),
        (OTHER_HOST, ROBOTS),
        (OTHER_HOST, "/articles/42"),
    ]
    assert resolver.calls == [ARTICLE_HOST, OTHER_HOST]
    assert connector.dialled == [PUBLIC_ADDRESS, SECOND_PUBLIC_ADDRESS]


async def test_relative_redirect_stays_on_the_host_and_reuses_its_robots_rules(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, "/old", redirect(ARTICLE_PATH))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article(f"https://{ARTICLE_HOST}/old"))

    assert upload.source_url == ARTICLE_URL
    assert site.paths() == [ROBOTS, "/old", ARTICLE_PATH]


async def test_http_link_is_fetched_over_https(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article(f"http://{ARTICLE_HOST}{ARTICLE_PATH}"))

    assert upload.source_url == ARTICLE_URL


async def test_http_link_to_a_site_that_does_not_answer_over_https_fails_clearly_without_retry(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, Reply(hang_up=True))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleUnavailableError, match=NO_SECURE_ANSWER_MESSAGE):
            await fetcher.fetch(article(f"http://{ARTICLE_HOST}{ARTICLE_PATH}"))


async def test_https_link_whose_site_drops_the_connection_is_a_retryable_upstream_failure(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, Reply(hang_up=True))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(UpstreamUnavailableError) as raised:
            await fetcher.fetch(article())

    assert raised.value.retry_after_seconds == FETCH_RETRY_AFTER_SECONDS


INTERNAL_ADDRESSES = [
    "127.0.0.1",
    METADATA_ADDRESS,
    "10.1.2.3",
    "172.16.5.4",
    "192.168.1.1",
    "100.64.0.1",
    "168.63.129.16",
    "::1",
    "::ffff:127.0.0.1",
    "fd00:ec2::254",
    "fe80::1",
]


@pytest.mark.parametrize("internal", INTERNAL_ADDRESSES)
async def test_host_resolving_to_an_internal_address_is_refused_and_the_address_sees_no_connection(
    tls_material: TlsMaterial, internal: str
) -> None:
    async with listening() as service:
        connector = RoutingConnector({internal: service.port})
        fetcher = article_fetcher(tls_material, resolving_hosts({ARTICLE_HOST: [[internal]]}), connector)

        with pytest.raises(ArticleUnavailableError, match=UNREACHABLE_MESSAGE):
            await fetcher.fetch(article())

    assert connector.dialled == []
    assert service.connections == 0
    assert refusals(fetcher, EgressRefusal.ADDRESS) == 1
    assert fetches(fetcher, ArticleFetchOutcome.UNAVAILABLE) == 1


async def test_positive_control_the_same_listener_is_reached_when_the_answer_is_public(
    tls_material: TlsMaterial,
) -> None:
    async with listening(reply=b"not tls") as service:
        connector = RoutingConnector({PUBLIC_ADDRESS: service.port})
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector)

        with pytest.raises(UpstreamUnavailableError):
            await fetcher.fetch(article())

    assert connector.dialled == [PUBLIC_ADDRESS]
    assert service.connections == 1
    assert refusals(fetcher, EgressRefusal.ADDRESS) == 0


async def test_any_internal_address_among_the_answers_refuses_the_host(tls_material: TlsMaterial) -> None:
    async with listening() as service, serving(tls_material) as site:
        serve_article(site)
        connector = RoutingConnector({PUBLIC_ADDRESS: site.port, "10.0.0.1": service.port})
        fetcher = article_fetcher(
            tls_material, resolving_hosts({ARTICLE_HOST: [[PUBLIC_ADDRESS, "10.0.0.1"]]}), connector
        )

        with pytest.raises(ArticleUnavailableError, match=UNREACHABLE_MESSAGE):
            await fetcher.fetch(article())

    assert connector.dialled == []
    assert service.connections == 0
    assert site.connections == 0


async def test_redirect_chain_ending_at_the_metadata_endpoint_never_reaches_it(
    tls_material: TlsMaterial,
) -> None:
    async with listening() as metadata, serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, redirect(f"https://{OTHER_HOST}/hop"))
        site.serve(OTHER_HOST, "/hop", redirect(f"https://{METADATA_HOST}{METADATA_PATH}"))
        resolver = resolving_hosts(
            {
                ARTICLE_HOST: [[PUBLIC_ADDRESS]],
                OTHER_HOST: [[SECOND_PUBLIC_ADDRESS]],
                METADATA_HOST: [[METADATA_ADDRESS]],
            }
        )
        connector = connector_to(site, **{METADATA_ADDRESS: metadata.port})
        fetcher = article_fetcher(tls_material, resolver, connector)

        with pytest.raises(ArticleUnavailableError, match=UNREACHABLE_MESSAGE):
            await fetcher.fetch(article())

    assert metadata.connections == 0
    assert METADATA_ADDRESS not in connector.dialled
    assert site.requests == [
        (ARTICLE_HOST, ROBOTS),
        (ARTICLE_HOST, ARTICLE_PATH),
        (OTHER_HOST, ROBOTS),
        (OTHER_HOST, "/hop"),
    ]
    assert refusals(fetcher, EgressRefusal.ADDRESS) == 1


@pytest.mark.parametrize(
    "location",
    [
        f"https://{METADATA_ADDRESS}{METADATA_PATH}",
        "https://[::1]/",
        "https://2852039166/latest/",
        "https://0xa9.0xfe.0xa9.0xfe/latest/",
        "https://localhost/admin",
        "https://metadata.google.internal/computeMetadata/v1/",
        f"http://{OTHER_HOST}/plain",
        f"https://{OTHER_HOST}:8443/admin",
        "https://user:password@other.example.org/",
        "ftp://other.example.org/file",
    ],
)
async def test_redirect_to_an_address_a_local_name_plain_http_or_another_port_is_never_attempted(
    tls_material: TlsMaterial, location: str
) -> None:
    async with listening() as internal, serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, redirect(location))
        resolver = public_dns(ARTICLE_HOST)
        connector = connector_to(site, **{METADATA_ADDRESS: internal.port, "127.0.0.1": internal.port})
        fetcher = article_fetcher(tls_material, resolver, connector)

        with pytest.raises(ArticleUnavailableError, match="redirects to an address that is not fetched"):
            await fetcher.fetch(article())

    assert resolver.calls == [ARTICLE_HOST]
    assert set(connector.dialled) == {PUBLIC_ADDRESS}
    assert internal.connections == 0


async def test_dns_rebinding_after_the_first_tunnel_is_refused_and_never_reaches_the_new_address(
    tls_material: TlsMaterial,
) -> None:
    async with listening() as internal, serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, Reply(status=HTTPStatus.NOT_FOUND, close=True))
        serve_article(site)
        resolver = resolving_hosts({ARTICLE_HOST: [[PUBLIC_ADDRESS], ["127.0.0.1"]]})
        connector = connector_to(site, **{"127.0.0.1": internal.port})
        fetcher = article_fetcher(tls_material, resolver, connector)

        with pytest.raises(ArticleUnavailableError, match=UNREACHABLE_MESSAGE):
            await fetcher.fetch(article())

    assert resolver.calls == [ARTICLE_HOST, ARTICLE_HOST]
    assert connector.dialled == [PUBLIC_ADDRESS]
    assert internal.connections == 0
    assert site.paths() == [ROBOTS]


async def test_robots_redirect_to_an_internal_host_is_refused_like_the_page_itself(
    tls_material: TlsMaterial,
) -> None:
    async with listening() as metadata, serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, redirect(f"https://{METADATA_HOST}/robots.txt"))
        serve_article(site)
        resolver = resolving_hosts({ARTICLE_HOST: [[PUBLIC_ADDRESS]], METADATA_HOST: [[METADATA_ADDRESS]]})
        connector = connector_to(site, **{METADATA_ADDRESS: metadata.port})
        fetcher = article_fetcher(tls_material, resolver, connector)

        with pytest.raises(ArticleUnavailableError, match=UNREACHABLE_MESSAGE):
            await fetcher.fetch(article())

    assert metadata.connections == 0
    assert site.paths() == [ROBOTS]


async def test_a_name_that_does_not_resolve_gets_exactly_the_answer_an_internal_address_gets(
    tls_material: TlsMaterial,
) -> None:
    connector = RoutingConnector({})
    unresolvable = article_fetcher(tls_material, resolving_hosts({}), connector)
    internal = article_fetcher(tls_material, resolving_hosts({ARTICLE_HOST: [[METADATA_ADDRESS]]}), connector)

    with pytest.raises(ArticleUnavailableError) as missing:
        await unresolvable.fetch(article())
    with pytest.raises(ArticleUnavailableError) as refused:
        await internal.fetch(article())

    assert str(missing.value) == str(refused.value) == UNREACHABLE_MESSAGE
    assert refusals(unresolvable, EgressRefusal.RESOLUTION) == 1
    assert refusals(internal, EgressRefusal.ADDRESS) == 1


async def test_endless_page_is_cut_at_the_byte_cap_instead_of_being_downloaded(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        start = article_page().removesuffix("</body></html>").encode()
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(body=start, endless=FILLER))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert ARTICLE_PARAGRAPHS[0] in upload.content.decode()
    assert truncations(fetcher, ArticleTruncation.BYTES) == 1
    assert site.sent_bytes < MAX_PAGE_BYTES + 16 * 1024 * 1024


async def test_page_declaring_a_gigabyte_is_cut_at_the_byte_cap(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        start = article_page().removesuffix("</body></html>").encode()
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(body=start, endless=FILLER, declared_length=1024**3))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert ARTICLE_PARAGRAPHS[-1] in upload.content.decode()
    assert truncations(fetcher, ArticleTruncation.BYTES) == 1
    assert site.sent_bytes < MAX_PAGE_BYTES + 16 * 1024 * 1024


async def test_gzip_bomb_is_inflated_only_up_to_the_byte_cap(tls_material: TlsMaterial) -> None:
    bomb = gzip.compress(article_page(padding=" " * (128 * 1024 * 1024)).encode(), compresslevel=9)
    async with serving(tls_material) as site:
        site.serve(
            ARTICLE_HOST,
            ARTICLE_PATH,
            Reply(headers=(("Content-Type", "text/html"), ("Content-Encoding", "gzip")), body=bomb),
        )
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        tracemalloc.start()
        try:
            upload = await fetcher.fetch(article())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert len(bomb) < MAX_PAGE_BYTES
    assert ARTICLE_PARAGRAPHS[0] in upload.content.decode()
    assert truncations(fetcher, ArticleTruncation.BYTES) == 1
    assert peak < 6 * MAX_PAGE_BYTES


async def test_compressed_page_within_the_cap_is_read_whole(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        site.serve(
            ARTICLE_HOST,
            ARTICLE_PATH,
            Reply(
                headers=(("Content-Type", HTML_TYPE), ("Content-Encoding", "gzip")),
                body=gzip.compress(article_bytes()),
            ),
        )
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert ARTICLE_PARAGRAPHS[-1] in upload.content.decode()
    assert truncations(fetcher, ArticleTruncation.BYTES) == 0


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ((("Content-Type", "text/html"), ("Content-Encoding", "br")), b"compressed"),
        ((("Content-Type", "text/html"), ("Content-Encoding", "gzip, gzip")), b"compressed"),
        ((("Content-Type", "text/html"), ("Content-Encoding", "gzip")), b"\x1f\x8b\x08\x00not a gzip stream"),
    ],
    ids=["brotli", "stacked", "damaged-gzip"],
)
async def test_unsupported_or_damaged_encoding_is_unavailable(
    tls_material: TlsMaterial, headers: tuple[tuple[str, str], ...], body: bytes
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(headers=headers, body=body))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleUnavailableError):
            await fetcher.fetch(article())


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/json", ""])
async def test_response_that_is_not_html_is_refused_from_its_headers_without_reading_the_body(
    tls_material: TlsMaterial, content_type: str
) -> None:
    async with serving(tls_material) as site:
        headers = (("Content-Type", content_type),) if content_type else ()
        site.serve(
            ARTICLE_HOST, ARTICLE_PATH, Reply(headers=headers, body=b"%PDF-1.7", endless=b"\0" * 65536)
        )
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(UnsupportedDocumentError, match="not a web page"):
            await fetcher.fetch(article())

    assert site.sent_bytes < 16 * 1024 * 1024
    assert fetches(fetcher, ArticleFetchOutcome.NOT_HTML) == 1


@pytest.mark.parametrize("content_type", ["text/html", "TEXT/HTML; charset=UTF-8", "application/xhtml+xml"])
async def test_html_and_xhtml_content_types_are_accepted(
    tls_material: TlsMaterial, content_type: str
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, page(article_bytes(), content_type))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == ARTICLE_TITLE


async def test_markup_past_the_node_cap_is_cut_before_extraction(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        padding = "<span>x</span>" * MAX_PAGE_NODES
        serve_article(site, article_page(padding=padding).encode())
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert ARTICLE_PARAGRAPHS[-1] in upload.content.decode()
    assert truncations(fetcher, ArticleTruncation.MARKUP) == 1


async def test_text_past_the_character_budget_is_cut_at_a_word(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        serve_article(site)
        limits = replace(TEST_LIMITS, max_characters=150)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site), limits)

        upload = await fetcher.fetch(article())

    text = upload.content.decode()
    assert len(text) <= 150
    assert "\n".join((ARTICLE_TITLE, *ARTICLE_PARAGRAPHS)).startswith(text)
    assert not text.endswith(" ")
    assert truncations(fetcher, ArticleTruncation.CHARACTERS) == 1


async def test_page_disallowed_by_robots_is_refused_and_never_requested(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, page(DISALLOW_SCIENCE, "text/plain"))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleBlockedByRobotsError):
            await fetcher.fetch(article())

    assert site.paths() == [ROBOTS]
    assert fetches(fetcher, ArticleFetchOutcome.BLOCKED_BY_ROBOTS) == 1


@pytest.mark.parametrize(
    "rules",
    [
        b"User-agent: *\nDisallow: /\n\nUser-agent: Deckly\nAllow: /\n",
        b"User-agent: *\nDisallow: /science/\nAllow: /science/spaced-repetition\n",
        b"User-agent: OtherBot\nDisallow: /\n",
        b"",
        b"<html>this is not a robots file</html>",
    ],
    ids=["own-group-allows", "longer-allow-wins", "other-agent-only", "empty", "html-instead"],
)
async def test_robots_rules_that_allow_the_page_let_it_through(
    tls_material: TlsMaterial, rules: bytes
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, page(rules, "text/plain"))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == ARTICLE_TITLE


@pytest.mark.parametrize(
    "status",
    [
        HTTPStatus.NOT_FOUND,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.GONE,
        HTTPStatus.TOO_MANY_REQUESTS,
    ],
)
async def test_robots_file_that_is_unavailable_allows_everything(
    tls_material: TlsMaterial, status: int
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, Reply(status=status, body=DISALLOW_SCIENCE))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == ARTICLE_TITLE


@pytest.mark.parametrize("status", [HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.SERVICE_UNAVAILABLE])
async def test_robots_file_that_is_unreachable_disallows_everything_and_is_retryable(
    tls_material: TlsMaterial, status: int
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, Reply(status=status))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(RobotsUnreachableError) as raised:
            await fetcher.fetch(article())

    assert raised.value.retry_after_seconds == FETCH_RETRY_AFTER_SECONDS
    assert site.paths() == [ROBOTS]
    assert fetches(fetcher, ArticleFetchOutcome.ROBOTS_UNREACHABLE) == 1


async def test_robots_redirect_to_another_host_is_followed_and_its_rules_apply(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, redirect(f"https://{ROBOTS_HOST}/rules.txt"))
        site.serve(ROBOTS_HOST, "/rules.txt", page(DISALLOW_SCIENCE, "text/plain"))
        serve_article(site)
        resolver = public_dns(ARTICLE_HOST, ROBOTS_HOST)
        fetcher = article_fetcher(tls_material, resolver, connector_to(site))

        with pytest.raises(ArticleBlockedByRobotsError):
            await fetcher.fetch(article())

    assert site.requests == [(ARTICLE_HOST, ROBOTS), (ROBOTS_HOST, "/rules.txt")]


async def test_robots_redirecting_more_than_five_times_counts_as_unavailable(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, redirect("/robots-0.txt"))
        for hop in range(MAX_ROBOTS_REDIRECTS + 1):
            site.serve(ARTICLE_HOST, f"/robots-{hop}.txt", redirect(f"/robots-{hop + 1}.txt"))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == ARTICLE_TITLE
    assert site.paths().count(ARTICLE_PATH) == 1


async def test_robots_redirect_to_plain_http_counts_as_unavailable(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, redirect(f"http://{ARTICLE_HOST}/robots.txt"))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == ARTICLE_TITLE


async def test_rules_past_the_first_five_hundred_kibibytes_of_robots_are_ignored(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        late = b"User-agent: *\n" + b"#" * MAX_ROBOTS_BYTES + b"\nDisallow: /science/\n"
        site.serve(ARTICLE_HOST, ROBOTS, page(late, "text/plain"))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == ARTICLE_TITLE


async def test_page_redirecting_to_a_host_whose_robots_disallow_it_is_refused_there(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, redirect(f"https://{OTHER_HOST}/science/copy"))
        site.serve(OTHER_HOST, ROBOTS, page(DISALLOW_SCIENCE, "text/plain"))
        site.serve(OTHER_HOST, "/science/copy", page(article_bytes()))
        resolver = public_dns(ARTICLE_HOST, OTHER_HOST)
        fetcher = article_fetcher(tls_material, resolver, connector_to(site))

        with pytest.raises(ArticleBlockedByRobotsError):
            await fetcher.fetch(article())

    assert site.paths(OTHER_HOST) == [ROBOTS]


@pytest.mark.parametrize(
    "status",
    [
        HTTPStatus.NOT_FOUND,
        HTTPStatus.GONE,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.PAYMENT_REQUIRED,
        HTTPStatus.UNAVAILABLE_FOR_LEGAL_REASONS,
        HTTPStatus.NO_CONTENT,
        HTTPStatus.NOT_MODIFIED,
    ],
)
async def test_page_status_the_client_cannot_fix_by_retrying_is_unavailable(
    tls_material: TlsMaterial, status: int
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(status=status))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleUnavailableError, match=f"HTTP {int(status)}"):
            await fetcher.fetch(article())


@pytest.mark.parametrize(
    "status",
    [
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.REQUEST_TIMEOUT,
    ],
)
async def test_page_status_that_may_pass_is_a_retryable_upstream_failure(
    tls_material: TlsMaterial, status: int
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(status=status))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(UpstreamUnavailableError) as raised:
            await fetcher.fetch(article())

    assert raised.value.retry_after_seconds == FETCH_RETRY_AFTER_SECONDS


async def test_redirect_loop_stops_after_five_redirects(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, redirect(ARTICLE_PATH))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleUnavailableError, match=f"more than {MAX_REDIRECTS} times"):
            await fetcher.fetch(article())

    assert site.paths().count(ARTICLE_PATH) == MAX_REDIRECTS + 1


async def test_redirect_without_a_location_is_unavailable(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(status=HTTPStatus.FOUND))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleUnavailableError, match="redirects to an address that is not fetched"):
            await fetcher.fetch(article())


async def test_page_rendered_by_javascript_fails_with_a_clear_error(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        serve_article(site, SINGLE_PAGE_APP.encode())
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleHasNoTextError, match="JavaScript"):
            await fetcher.fetch(article())

    assert fetches(fetcher, ArticleFetchOutcome.NO_TEXT) == 1


async def test_page_that_renders_its_article_on_the_server_alongside_scripts_is_extracted(
    tls_material: TlsMaterial,
) -> None:
    scripts = '<script>window.__STATE__ = {"route": "/science"};</script><script src=\'/app.js\'></script>'
    async with serving(tls_material) as site:
        serve_article(site, article_page(padding=scripts).encode())
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert "__STATE__" not in upload.content.decode()
    assert ARTICLE_PARAGRAPHS[2] in upload.content.decode()


async def test_page_without_text_or_title_has_no_text(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        serve_article(site, f"<html><body>{NAVIGATION}{COOKIE_BANNER}{FOOTER}</body></html>".encode())
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleHasNoTextError):
            await fetcher.fetch(article())


async def test_charset_from_the_content_type_decodes_a_legacy_encoded_page(tls_material: TlsMaterial) -> None:
    body = article_page(title=RUSSIAN_TITLE, paragraphs=RUSSIAN_PARAGRAPHS).replace(
        "<meta charset='utf-8'>", ""
    )
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, page(body.encode("cp1251"), "text/html; charset=windows-1251"))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == RUSSIAN_TITLE
    assert RUSSIAN_PARAGRAPHS[0] in upload.content.decode()


async def test_page_without_a_title_is_named_after_its_host(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        serve_article(site, article_page(title=None).encode())
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.filename == ARTICLE_HOST


async def test_title_with_path_separators_cannot_become_a_path(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        serve_article(site, article_page(title="../../etc/passwd \\ evil").encode())
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert "/" not in upload.filename
    assert "\\" not in upload.filename


async def test_certificate_that_does_not_name_the_host_is_refused(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        serve_article(site, host=UNCERTIFIED_HOST)
        fetcher = article_fetcher(tls_material, public_dns(UNCERTIFIED_HOST), connector_to(site))

        with pytest.raises(ArticleUnavailableError, match="secure connection could not be verified"):
            await fetcher.fetch(article(f"https://{UNCERTIFIED_HOST}{ARTICLE_PATH}"))

    assert site.requests == []


async def test_page_that_takes_longer_than_the_fetch_deadline_is_a_timed_out_upstream_failure(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(body=article_bytes(), stall_seconds=5))
        limits = replace(TEST_LIMITS, timeout_seconds=0.5)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site), limits)

        with pytest.raises(UpstreamUnavailableError) as raised:
            await fetcher.fetch(article())

    assert "longer than 0.5 seconds" in " ".join(raised.value.__notes__)
    assert fetches(fetcher, ArticleFetchOutcome.TIMED_OUT) == 1


async def test_page_that_stalls_past_one_request_timeout_is_a_retryable_upstream_failure(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(body=article_bytes(), stall_seconds=5))
        limits = replace(TEST_LIMITS, request_timeout_seconds=0.3)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site), limits)

        with pytest.raises(UpstreamUnavailableError) as raised:
            await fetcher.fetch(article())

    assert not isinstance(raised.value, ArticleEgressRefusedError)
    assert fetches(fetcher, ArticleFetchOutcome.FAILED) == 1


@pytest.mark.parametrize(
    "reason",
    [EgressRefusal.TUNNELS, EgressRefusal.BYTES, EgressRefusal.METHOD, EgressRefusal.CREDENTIALS],
)
def test_proxy_refusing_for_its_own_budgets_is_an_egress_refusal_not_an_unreachable_page(
    reason: EgressRefusal,
) -> None:
    session = EgressSession("token", EgressLimits(1, 1, 1.0, 1.0, 1.0))
    session.refusals.append(reason)

    failure = transport_failure(httpx_error(), FetchAttempt(article=article(), session=session))

    assert isinstance(failure, ArticleEgressRefusedError)


def test_proxy_failing_to_connect_to_a_public_address_is_a_plain_retryable_failure() -> None:
    session = EgressSession("token", EgressLimits(1, 1, 1.0, 1.0, 1.0))
    session.refusals.append(EgressRefusal.UNREACHABLE)

    failure = transport_failure(httpx_error(), FetchAttempt(article=article(), session=session))

    assert type(failure) is UpstreamUnavailableError


def httpx_error() -> httpx2.HTTPError:
    return httpx2.ProxyError("refused")


async def test_redirect_to_a_raw_utf8_path_is_percent_encoded_and_followed_like_a_browser(
    tls_material: TlsMaterial,
) -> None:
    encoded = "/%D1%81%D1%82%D0%B0%D1%82%D1%8C%D1%8F"
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, redirect("/статья"))
        site.serve(ARTICLE_HOST, encoded, page(article_bytes()))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article())

    assert upload.source_url == f"https://{ARTICLE_HOST}{encoded}"


async def test_failure_detail_written_to_the_log_is_bounded_even_when_the_site_controls_it(
    tls_material: TlsMaterial,
) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, Reply(headers=(("Content-Type", "x" * 5000),)))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with captured_json_logs() as logs, pytest.raises(UnsupportedDocumentError):
            await fetcher.fetch(article())

    [failure] = logs.named("article_url_fetch_failed")
    detail = failure["detail"]
    assert isinstance(detail, str)
    assert len(detail) == MAX_LOGGED_ERROR_CHARACTERS


async def test_page_exactly_at_the_byte_cap_is_whole_and_one_byte_more_is_cut(
    tls_material: TlsMaterial,
) -> None:
    body = article_bytes()
    async with serving(tls_material) as site:
        serve_article(site, body)
        exact = replace(TEST_LIMITS, max_page_bytes=len(body))
        short = replace(TEST_LIMITS, max_page_bytes=len(body) - 1)
        whole = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site), exact)
        cut = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site), short)

        await whole.fetch(article())
        await cut.fetch(article())

    assert truncations(whole, ArticleTruncation.BYTES) == 0
    assert truncations(cut, ArticleTruncation.BYTES) == 1


async def test_exactly_five_redirects_are_followed(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        for hop in range(MAX_REDIRECTS):
            site.serve(ARTICLE_HOST, f"/hop-{hop}", redirect(f"/hop-{hop + 1}"))
        site.serve(ARTICLE_HOST, f"/hop-{MAX_REDIRECTS}", page(article_bytes()))
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        upload = await fetcher.fetch(article(f"https://{ARTICLE_HOST}/hop-0"))

    assert upload.source_url == f"https://{ARTICLE_HOST}/hop-{MAX_REDIRECTS}"


async def test_robots_reached_after_exactly_five_redirects_still_applies(tls_material: TlsMaterial) -> None:
    async with serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ROBOTS, redirect("/robots-1.txt"))
        for hop in range(1, MAX_ROBOTS_REDIRECTS):
            site.serve(ARTICLE_HOST, f"/robots-{hop}.txt", redirect(f"/robots-{hop + 1}.txt"))
        site.serve(ARTICLE_HOST, f"/robots-{MAX_ROBOTS_REDIRECTS}.txt", page(DISALLOW_SCIENCE, "text/plain"))
        serve_article(site)
        fetcher = article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector_to(site))

        with pytest.raises(ArticleBlockedByRobotsError):
            await fetcher.fetch(article())


def test_text_exactly_at_the_character_budget_is_kept_whole_and_not_counted_as_cut() -> None:
    text = "word " * 19 + "last"

    kept = article_text(ExtractedArticle(title="t", text=text), article(), len(text))
    cut = article_text(ExtractedArticle(title="t", text=text), article(), len(text) - 1)

    assert kept.upload.content.decode() == text
    assert kept.truncated is False
    assert cut.truncated is True
    assert cut.upload.content.decode() == "word " * 18 + "word"


def test_page_needs_fifty_visible_characters_of_text() -> None:
    enough = "a" * 25 + " " * 10 + "b" * 25
    short = "a" * 24 + " " * 10 + "b" * 25

    assert article_text(ExtractedArticle(title="t", text=enough), article(), 1000).upload.content
    with pytest.raises(ArticleHasNoTextError):
        article_text(ExtractedArticle(title="t", text=short), article(), 1000)


def test_unstorable_characters_from_the_extractor_never_reach_storage() -> None:
    text = "Spaced repetition \x00 spreads \ud800 reviews out over growing intervals of time for memory."

    stored = article_text(ExtractedArticle(title="t", text=text), article(), 1000).upload.content.decode()

    assert "\x00" not in stored
    assert "\ud800" not in stored
