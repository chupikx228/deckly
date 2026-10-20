import pytest

from deckly.domain.article_url import (
    MAX_ARTICLE_URL_LENGTH,
    MAX_HOST_LENGTH,
    ArticleUrl,
    is_public_host_name,
    parse_article_url,
    parse_redirect_url,
)
from deckly.domain.exceptions import InvalidDocumentError, UnsupportedArticleUrlError


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        (
            "https://en.wikipedia.org/wiki/Spaced_repetition",
            "https://en.wikipedia.org/wiki/Spaced_repetition",
        ),
        ("  https://example.com/a?b=c&d=e#section  ", "https://example.com/a?b=c&d=e"),
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com?q=1", "https://example.com/?q=1"),
        ("https://example.com.:443/a", "https://example.com/a"),
        ("http://paulgraham.com/greatwork.html", "https://paulgraham.com/greatwork.html"),
        ("http://example.com:80/a", "https://example.com/a"),
        (
            "https://xn--d1acufc.xn--p1ai/%D1%81%D1%82%D0%B0%D1%82%D1%8C%D1%8F",
            "https://xn--d1acufc.xn--p1ai/%D1%81%D1%82%D0%B0%D1%82%D1%8C%D1%8F",
        ),
        ("https://a-b.c-d.co.uk/x", "https://a-b.c-d.co.uk/x"),
        ("https://1.2.3.4.example.com/", "https://1.2.3.4.example.com/"),
    ],
)
def test_public_web_link_is_kept_as_its_https_form(raw: str, url: str) -> None:
    assert parse_article_url(raw).url == url


def test_http_link_is_marked_as_upgraded_and_https_one_is_not() -> None:
    assert parse_article_url("http://example.com/a").upgraded is True
    assert parse_article_url("https://example.com/a").upgraded is False


def test_origin_is_the_https_host_without_the_path() -> None:
    assert parse_article_url("https://example.com/a/b?c").origin == "https://example.com"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "example.com/article",
        "//example.com/article",
        "/relative/path",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<p>x</p>",
        "gopher://example.com/",
        "https://127.0.0.1/",
        "https://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "https://[fd00:ec2::254]/",
        "https://2130706433/",
        "https://0x7f.0.0.1/",
        "https://0x7f000001/",
        "https://017700000001/",
        "https://127.1/",
        "https://1.2.3.4.5/",
        "https://localhost/",
        "https://LOCALHOST/",
        "https://api.localhost/",
        "https://printer.local/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://router.home.arpa/",
        "https://1.0.0.127.in-addr.arpa/",
        "https://site.test/",
        "https://site.invalid/",
        "https://example/",
        "https://hiddenservice.onion/",
        "https://kubernetes/",
        "https://intranet/",
        "https://user:password@example.com/",
        "https://user@example.com/",
        "https://example.com:8443/",
        "https://example.com:80/",
        "http://example.com:443/",
        "https://example.com:0/",
        "https://exa_mple.com/",
        "https://-example.com/",
        "https://example-.com/",
        "https://example..com/",
        "https://.example.com/",
        "https://example.com../",
        "https://example.c/",
        "https://example.123/",
        "https://пример.рф/",
        "https://example.com/путь",
        "https://example.com/a b",
        "https://example.com/\x00",
        "https://example.com\t/",
        "https://exam ple.com/",
        "https://example.com\\@evil.com/",
        f"https://{'a' * 64}.com/",
    ],
)
def test_link_that_is_not_a_public_web_page_is_refused_before_anything_is_fetched(raw: str) -> None:
    with pytest.raises(UnsupportedArticleUrlError):
        parse_article_url(raw)


def test_refusal_is_an_invalid_document_so_nothing_is_counted_or_stored() -> None:
    with pytest.raises(InvalidDocumentError):
        parse_article_url("https://localhost/")


def test_redirect_target_must_already_be_https() -> None:
    assert parse_redirect_url("https://example.com/next").url == "https://example.com/next"
    with pytest.raises(UnsupportedArticleUrlError, match="must be an https link"):
        parse_redirect_url("http://example.com/next")


def test_redirect_target_is_not_trimmed_like_a_typed_link() -> None:
    with pytest.raises(UnsupportedArticleUrlError):
        parse_redirect_url(" https://example.com/next")


def test_link_exactly_at_the_length_limit_is_read_and_one_longer_is_refused() -> None:
    prefix = "https://example.com/"
    exact = prefix + "a" * (MAX_ARTICLE_URL_LENGTH - len(prefix))

    assert parse_article_url(exact).url == exact
    with pytest.raises(UnsupportedArticleUrlError, match=str(MAX_ARTICLE_URL_LENGTH)):
        parse_article_url(exact + "a")


def test_upgraded_link_cannot_grow_past_the_length_limit() -> None:
    prefix = "http://example.com/"
    at_limit = prefix + "a" * (MAX_ARTICLE_URL_LENGTH - len(prefix))

    with pytest.raises(UnsupportedArticleUrlError, match=str(MAX_ARTICLE_URL_LENGTH)):
        parse_article_url(at_limit)


def test_host_at_its_length_limit_is_public_and_one_longer_is_not() -> None:
    labels = ["a" * 63, "b" * 63, "c" * 63, "d" * 57, "com"]
    host = ".".join(labels)

    assert len(host) == MAX_HOST_LENGTH
    assert is_public_host_name(host)
    assert not is_public_host_name("e" + host)


@pytest.mark.parametrize("target", ["", "relative", "/a#fragment", "/a b", "/é"])
def test_article_url_refuses_a_target_that_is_not_an_absolute_visible_path(target: str) -> None:
    with pytest.raises(UnsupportedArticleUrlError, match="path must start with /"):
        ArticleUrl(host="example.com", target=target)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "metadata.google.internal", "Example.com"])
def test_article_url_refuses_a_host_that_is_not_a_public_lowercase_name(host: str) -> None:
    with pytest.raises(UnsupportedArticleUrlError, match="public domain name"):
        ArticleUrl(host=host, target="/")
