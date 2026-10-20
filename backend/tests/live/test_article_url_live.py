import pytest

from deckly.config import MAX_DOCUMENT_SOURCE_CHARACTERS
from deckly.domain.article_url import parse_article_url
from deckly.domain.exceptions import ArticleBlockedByRobotsError, ArticleUnavailableError
from deckly.infrastructure.article_url.fetcher import (
    UNREACHABLE_MESSAGE,
    ArticleFetchLimits,
    ArticleFetchOutcome,
    build_article_fetcher,
)
from tests.fakes import fresh_metrics
from tests.logs import captured_json_logs

pytestmark = [pytest.mark.live, pytest.mark.anyio]

ARTICLE = "https://en.wikipedia.org/wiki/Spaced_repetition"
ROBOTS_DISALLOWED = "https://www.google.com/search?q=spaced+repetition"
METADATA_BY_PUBLIC_DNS = "https://169.254.169.254.nip.io/latest/meta-data/"
LIMITS = ArticleFetchLimits(max_characters=MAX_DOCUMENT_SOURCE_CHARACTERS, timeout_seconds=30)


async def test_wikipedia_article_is_fetched_through_the_egress_proxy_and_extracted() -> None:
    metrics = fresh_metrics()
    fetcher = build_article_fetcher(LIMITS, metrics)

    with captured_json_logs() as logs:
        upload = await fetcher.fetch(parse_article_url(ARTICLE))

    text = upload.content.decode()
    assert upload.source_url == ARTICLE
    assert "Spaced repetition" in upload.filename
    assert "flashcards" in text
    assert "Jump to content" not in text
    assert (
        metrics.registry.get_sample_value(
            "deckly_article_url_fetches_total", {"outcome": ArticleFetchOutcome.FETCHED}
        )
        == 1
    )
    assert logs.named("article_url_egress_refused") == []


async def test_page_disallowed_by_its_robots_file_is_refused() -> None:
    fetcher = build_article_fetcher(LIMITS, fresh_metrics())

    with pytest.raises(ArticleBlockedByRobotsError):
        await fetcher.fetch(parse_article_url(ROBOTS_DISALLOWED))


async def test_public_name_resolving_to_the_metadata_address_is_refused_by_the_real_resolver() -> None:
    fetcher = build_article_fetcher(LIMITS, fresh_metrics())

    with captured_json_logs() as logs, pytest.raises(ArticleUnavailableError, match=UNREACHABLE_MESSAGE):
        await fetcher.fetch(parse_article_url(METADATA_BY_PUBLIC_DNS))

    [refusal] = logs.named("article_url_egress_refused")
    assert refusal["reason"] == "address"
