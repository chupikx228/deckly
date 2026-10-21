import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from deckly.infrastructure.article_url.extracted import MAX_PAGE_ATTRIBUTES, MAX_PAGE_NODES
from deckly.infrastructure.article_url.extractor import (
    decoded,
    extract,
    main,
    markup_end,
    parse_arguments,
)
from tests.article_url import (
    ARTICLE_PARAGRAPHS,
    ARTICLE_TITLE,
    RUSSIAN_PARAGRAPHS,
    RUSSIAN_TITLE,
    SINGLE_PAGE_APP,
    article_bytes,
    article_page,
)
from tests.timing import LINEAR_COST_SCALE, MAX_LINEAR_COST_GROWTH, cpu_costs

MAX_CHARACTERS = 10_000


def test_article_text_and_title_are_extracted_without_navigation_banners_or_footers() -> None:
    extracted = extract(article_bytes(), None, MAX_CHARACTERS)

    assert extracted.title == ARTICLE_TITLE
    assert extracted.text is not None
    assert all(paragraph in extracted.text for paragraph in ARTICLE_PARAGRAPHS)
    for boilerplate in ("Sign in to your account", "We use cookies", "All rights reserved"):
        assert boilerplate not in extracted.text
    assert extracted.markup_truncated is False


def test_shell_of_a_javascript_application_has_no_text() -> None:
    extracted = extract(SINGLE_PAGE_APP.encode(), None, MAX_CHARACTERS)

    assert extracted.text is None


def test_text_is_cut_to_the_character_limit() -> None:
    extracted = extract(article_bytes(), None, 40)

    assert extracted.text is not None
    assert len(extracted.text) == 40


def test_declared_charset_decodes_a_legacy_page() -> None:
    page = article_page(title=RUSSIAN_TITLE, paragraphs=RUSSIAN_PARAGRAPHS).replace(
        "<meta charset='utf-8'>", ""
    )

    extracted = extract(page.encode("cp1251"), "windows-1251", MAX_CHARACTERS)

    assert extracted.title == RUSSIAN_TITLE
    assert extracted.text is not None
    assert RUSSIAN_PARAGRAPHS[1] in extracted.text


def test_byte_order_mark_wins_over_a_declared_charset() -> None:
    content = b"\xef\xbb\xbf" + "привет".encode()

    assert decoded(content, "windows-1251") == content


@pytest.mark.parametrize("charset", [None, "no-such-charset", "base64", "zlib"])
def test_missing_unknown_or_non_text_charset_leaves_detection_to_the_extractor(charset: str | None) -> None:
    assert decoded(b"<p>x</p>", charset) == b"<p>x</p>"


def test_markup_within_both_caps_is_kept_whole() -> None:
    content = b"<p class='a' id=b>text</p>" * 10

    assert markup_end(content, MAX_PAGE_NODES, MAX_PAGE_ATTRIBUTES) == len(content)


def test_markup_is_cut_at_the_node_past_the_node_cap() -> None:
    content = b"<i>x</i>" * 10

    assert markup_end(content, 5, MAX_PAGE_ATTRIBUTES) == len(b"<i>x</i><i>x</i><i>x")


def test_markup_is_cut_at_the_tag_whose_attributes_pass_the_attribute_cap() -> None:
    first = b"<a href='/x' class=\"one two three\" data-x>link</a>"
    second = b"<b " + b" ".join(b"flag%d" % number for number in range(10)) + b">bold</b>"

    assert markup_end(first + second, MAX_PAGE_NODES, 3) == len(first) + 0
    assert markup_end(first + second, MAX_PAGE_NODES, 13) == len(first + second)


def test_boolean_attributes_count_towards_the_attribute_cap() -> None:
    tag = b"<span " + b" ".join(b"b%d" % number for number in range(100)) + b">t</span>"

    assert markup_end(tag * 3, MAX_PAGE_NODES, 250) == 2 * len(tag)


def scanning(content: bytes) -> Callable[[], int]:
    return lambda: markup_end(content, MAX_PAGE_NODES, MAX_PAGE_ATTRIBUTES)


def tag_filled_with(filler: bytes, size: int) -> bytes:
    return b"<a" + filler * (size // len(filler)) + b">"


def test_markup_without_a_closing_bracket_is_scanned_in_linear_time() -> None:
    content = b"<a" * (1024 * 1024)

    smaller_page, whole_page = cpu_costs(
        scanning(content[: len(content) // LINEAR_COST_SCALE]), scanning(content)
    )

    assert whole_page < smaller_page * MAX_LINEAR_COST_GROWTH
    assert scanning(content)() == 2 * MAX_PAGE_NODES


def test_many_attributes_are_cut_before_the_html_parser_sees_them() -> None:
    tag = b"<span " + b" ".join(b"b%d" % number for number in range(2000)) + b">t</span>"
    page = article_bytes().replace(b"</article>", b"</article>" + tag * 100)

    extracted = extract(page, None, MAX_CHARACTERS)

    assert extracted.markup_truncated is True
    assert extracted.text is not None
    assert ARTICLE_PARAGRAPHS[0] in extracted.text


def test_main_writes_one_json_document(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_bytes(article_bytes())
    out = io.StringIO()

    main(["--max-characters=10000", "--", str(page)], out=out)

    written = json.loads(out.getvalue())
    assert written["title"] == ARTICLE_TITLE
    assert written["markup_truncated"] is False


def test_charset_argument_is_passed_through(tmp_path: Path) -> None:
    arguments = parse_arguments(["--charset=windows-1251", "--max-characters=5", "--", str(tmp_path / "p")])

    assert arguments.charset == "windows-1251"
    assert arguments.max_characters == 5


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_character_limit_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(SystemExit):
        parse_arguments([f"--max-characters={value}", "--", "page.html"])


@pytest.mark.parametrize("filler", [b" ", b"/", b" /", b" =", b' a="', b" a= "])
def test_tag_filled_with_whitespace_or_dangling_attributes_is_counted_in_linear_time(filler: bytes) -> None:
    content = tag_filled_with(filler, 128 * 1024)
    smaller = tag_filled_with(filler, 128 * 1024 // LINEAR_COST_SCALE)

    smaller_tag, whole_tag = cpu_costs(scanning(smaller), scanning(content))

    assert whole_tag < smaller_tag * MAX_LINEAR_COST_GROWTH


def test_markup_with_exactly_the_node_cap_is_kept_whole() -> None:
    content = b"<i>x</i>" * 10

    assert markup_end(content, 20, MAX_PAGE_ATTRIBUTES) == len(content)
    assert markup_end(content, 19, MAX_PAGE_ATTRIBUTES) == len(content) - len(b"</i>")
