import importlib.util
import sys
from pathlib import Path

import pytest

from deckly.domain.exceptions import ArticleUnavailableError
from deckly.infrastructure.article_url.extraction import (
    ExtractorInvocation,
    SubprocessArticleExtractor,
    require_extractor,
    safe_charset,
)
from deckly.infrastructure.article_url.fetcher import ArticleFetchLimits, build_article_fetcher
from tests.article_url import ARTICLE_PARAGRAPHS, ARTICLE_TITLE, SINGLE_PAGE_APP, article_bytes
from tests.fakes import fresh_metrics

pytestmark = pytest.mark.anyio

MAX_CHARACTERS = 10_000


def scripted(script: str, deadline_seconds: float = 10.0) -> SubprocessArticleExtractor:
    command = (sys.executable, "-I", "-c", script)
    return SubprocessArticleExtractor(
        ExtractorInvocation(max_characters=MAX_CHARACTERS, command=command), deadline_seconds=deadline_seconds
    )


def real_extractor() -> SubprocessArticleExtractor:
    return SubprocessArticleExtractor(ExtractorInvocation(max_characters=MAX_CHARACTERS), deadline_seconds=30)


async def test_extractor_runs_in_its_own_process_and_answers_with_the_article() -> None:
    extracted = await real_extractor().extract(article_bytes(), "utf-8")

    assert extracted.title == ARTICLE_TITLE
    assert extracted.text is not None
    assert ARTICLE_PARAGRAPHS[0] in extracted.text


async def test_extractor_process_reports_no_text_for_a_javascript_shell() -> None:
    extracted = await real_extractor().extract(SINGLE_PAGE_APP.encode(), None)

    assert extracted.text is None


async def test_extractor_that_hangs_is_killed_at_its_deadline() -> None:
    extractor = scripted("import time; time.sleep(60)", deadline_seconds=0.5)

    with pytest.raises(ArticleUnavailableError) as raised:
        await extractor.extract(b"<p>x</p>", None)

    assert "did not finish within 0.5 seconds" in " ".join(raised.value.__notes__)


async def test_extractor_writing_more_than_its_output_cap_is_stopped() -> None:
    extractor = scripted("import sys\nwhile True: sys.stdout.write('x' * 65536)")

    with pytest.raises(ArticleUnavailableError) as raised:
        await extractor.extract(b"<p>x</p>", None)

    assert "wrote more than" in " ".join(raised.value.__notes__)


async def test_extractor_that_crashes_is_an_unreadable_page() -> None:
    extractor = scripted("import sys; sys.stderr.write('boom'); sys.exit(3)")

    with pytest.raises(ArticleUnavailableError, match="could not be read") as raised:
        await extractor.extract(b"<p>x</p>", None)

    assert "exited 3: boom" in " ".join(raised.value.__notes__)


@pytest.mark.parametrize(
    "output", ["not json", '{"title": 1, "text": null}', '{"title": null, "text": null, "extra": 1}']
)
async def test_extractor_answer_in_an_unexpected_shape_is_an_unreadable_page(output: str) -> None:
    extractor = scripted(f"import sys; sys.stdout.write({output!r})")

    with pytest.raises(ArticleUnavailableError) as raised:
        await extractor.extract(b"<p>x</p>", None)

    assert "unexpected shape" in " ".join(raised.value.__notes__)


def test_page_and_charset_are_passed_as_arguments_that_cannot_become_options(tmp_path: Path) -> None:
    page = tmp_path / "page.html"

    arguments = ExtractorInvocation(max_characters=7).arguments(page, "--max-characters=1")

    assert arguments[-2:] == ["--", str(page)]
    assert "--max-characters=7" in arguments
    assert not [argument for argument in arguments if argument.startswith("--charset")]


@pytest.mark.parametrize(
    ("charset", "kept"),
    [
        ("utf-8", "utf-8"),
        ("windows-1251", "windows-1251"),
        ("ISO_8859-1:1987", "ISO_8859-1:1987"),
        (None, None),
        ("", None),
        ("-x", None),
        ("utf 8", None),
        ("a" * 41, None),
        ("utf-8\n--max-characters=1", None),
    ],
)
def test_only_a_plain_charset_name_reaches_the_extractor(charset: str | None, kept: str | None) -> None:
    assert safe_charset(charset) == kept


def test_extractor_environment_has_no_path_and_a_private_home(tmp_path: Path) -> None:
    environment = ExtractorInvocation.environment(tmp_path)

    assert environment["PATH"] == ""
    assert environment["HOME"] == environment["TMPDIR"] == str(tmp_path)


def test_fetcher_refuses_to_start_without_the_extractor_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)

    with pytest.raises(RuntimeError, match="trafilatura"):
        build_article_fetcher(ArticleFetchLimits(max_characters=10, timeout_seconds=1), fresh_metrics())


def test_extractor_package_is_installed() -> None:
    require_extractor()
