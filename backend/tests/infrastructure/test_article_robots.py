from http import HTTPStatus

import pytest

from deckly.infrastructure.article_url.robots import (
    ALLOW_ALL,
    MAX_ROBOTS_BYTES,
    ROBOTS_RETRY_AFTER_SECONDS,
    RobotsUnreachableError,
    robots_rules,
)

PAGE = "https://example.com/science/spaced-repetition"


def allows(rules: str, url: str = PAGE) -> bool:
    return robots_rules(HTTPStatus.OK, rules.encode()).allows(url)


@pytest.mark.parametrize(
    ("rules", "url"),
    [
        ("User-agent: *\nDisallow: /history/", PAGE),
        ("User-agent: *\nDisallow: /science/\nAllow: /science/spaced", PAGE),
        ("User-agent: *\nAllow: /science/spaced\nDisallow: /science/", PAGE),
        ("User-agent: *\nDisallow: /*.pdf$", "https://example.com/paper.pdfx"),
        ("User-agent: *\nDisallow: /\n\nUser-agent: Deckly\nAllow: /", PAGE),
        ("User-agent: GPTBot\nDisallow: /", PAGE),
        ("", PAGE),
        ("<html><body>not robots</body></html>", PAGE),
    ],
)
def test_rules_that_leave_the_page_open_allow_it(rules: str, url: str) -> None:
    assert allows(rules, url)


@pytest.mark.parametrize(
    ("rules", "url"),
    [
        ("User-agent: *\nDisallow: /", PAGE),
        ("User-agent: *\nDisallow: /science/", PAGE),
        ("User-agent: *\nDisallow: /*.pdf$", "https://example.com/paper.pdf"),
        ("User-agent: *\nDisallow: /*?print=", "https://example.com/a?print=1"),
        ("User-agent: *\nAllow: /\n\nUser-agent: Deckly\nDisallow: /science/", PAGE),
        ("User-agent: deckly\nDisallow: /", PAGE),
    ],
)
def test_rules_that_close_the_page_to_deckly_disallow_it(rules: str, url: str) -> None:
    assert not allows(rules, url)


def test_only_the_first_five_hundred_kibibytes_are_read() -> None:
    late = "User-agent: *\n" + "#" * MAX_ROBOTS_BYTES + "\nDisallow: /"

    assert allows(late)


def test_byte_order_mark_and_undecodable_bytes_do_not_stop_the_rules_from_being_read() -> None:
    rules = robots_rules(HTTPStatus.OK, b"\xef\xbb\xbfUser-agent: *\nDisallow: /science/\n# \xff\xfe\n")

    assert not rules.allows(PAGE)


@pytest.mark.parametrize(
    "status",
    [
        HTTPStatus.NOT_FOUND,
        HTTPStatus.GONE,
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.NOT_MODIFIED,
    ],
)
def test_robots_file_that_is_unavailable_allows_everything(status: int) -> None:
    assert robots_rules(status, b"User-agent: *\nDisallow: /") is ALLOW_ALL
    assert ALLOW_ALL.allows(PAGE)


@pytest.mark.parametrize(
    "status", [HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.BAD_GATEWAY, HTTPStatus.SERVICE_UNAVAILABLE, 599]
)
def test_robots_file_that_is_unreachable_means_complete_disallow_until_retried(status: int) -> None:
    with pytest.raises(RobotsUnreachableError) as raised:
        robots_rules(status, b"")

    assert raised.value.retry_after_seconds == ROBOTS_RETRY_AFTER_SECONDS
    assert f"HTTP {status}" in " ".join(raised.value.__notes__)
