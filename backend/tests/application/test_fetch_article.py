from dataclasses import dataclass, field, replace

import pytest

from deckly.application.documents import FetchArticle
from deckly.application.ports import DocumentUpload, Requester
from deckly.domain.article_url import ArticleUrl, parse_article_url
from deckly.domain.document import DocumentFormat
from deckly.domain.exceptions import (
    ArticleUnavailableError,
    DocumentTooLargeError,
    UnsupportedArticleUrlError,
    UnsupportedDocumentError,
)
from deckly.infrastructure.article_url.fetcher import ArticleTextInspector
from deckly.infrastructure.documents.store import DocumentStatus
from deckly.infrastructure.observability.metrics import ARTICLE_URL_GATE
from deckly.infrastructure.upload_gate import RejectingUploadGate
from tests.documents import ADDRESS, CLIENT_ID, DocumentHarness

pytestmark = pytest.mark.anyio

URL = "http://en.wikipedia.org/wiki/Spaced_repetition#History"
FINAL_URL = "https://en.wikipedia.org/wiki/Spaced_repetition"
TEXT = "Spaced repetition is an evidence-based learning technique usually performed with flashcards."
REQUESTER = Requester(client_id=CLIENT_ID, address=ADDRESS)
MAX_BYTES = 1024


@dataclass
class ScriptedArticles:
    upload: DocumentUpload = field(
        default_factory=lambda: DocumentUpload(
            filename="Spaced repetition - Wikipedia", content=TEXT.encode(), source_url=FINAL_URL
        )
    )
    failure: Exception | None = None
    fetched: list[ArticleUrl] = field(default_factory=list)

    async def fetch(self, article: ArticleUrl) -> DocumentUpload:
        self.fetched.append(article)
        if self.failure is not None:
            raise self.failure
        return self.upload


def harness() -> tuple[DocumentHarness, ScriptedArticles, FetchArticle]:
    documents = DocumentHarness(max_bytes=MAX_BYTES, concurrency=1)
    articles = ScriptedArticles()
    fetch = FetchArticle(
        upload=replace(
            documents.upload,
            gate=RejectingUploadGate(documents.slots, ARTICLE_URL_GATE, documents.metrics),
            inspector=ArticleTextInspector(),
        ),
        fetcher=articles,
    )
    return documents, articles, fetch


async def test_fetched_page_is_stored_as_text_that_remembers_where_it_came_from() -> None:
    documents, articles, fetch = harness()

    stored = await fetch(REQUESTER, URL)

    assert articles.fetched == [parse_article_url(URL)]
    assert stored.document_format is DocumentFormat.TEXT
    assert stored.source_url == FINAL_URL
    assert stored.filename == "Spaced repetition - Wikipedia"
    assert stored.size_bytes == len(TEXT.encode())
    assert stored.duration_seconds is None
    assert documents.store.rows[stored.document_id].status is DocumentStatus.STORED
    assert documents.storage.objects[stored.storage_key] == (TEXT.encode(), DocumentFormat.TEXT)
    assert documents.limiter.used[CLIENT_ID] == 1


@pytest.mark.parametrize("url", ["https://localhost/", "https://169.254.169.254/", "ftp://example.com/x"])
async def test_link_that_is_not_a_public_web_page_is_refused_before_it_is_counted_or_fetched(
    url: str,
) -> None:
    documents, articles, fetch = harness()

    with pytest.raises(UnsupportedArticleUrlError):
        await fetch(REQUESTER, url)

    assert articles.fetched == []
    assert documents.limiter.used[CLIENT_ID] == 0


async def test_page_the_fetcher_cannot_read_is_counted_and_nothing_is_stored() -> None:
    documents, articles, fetch = harness()
    articles.failure = ArticleUnavailableError("the page could not be reached")

    with pytest.raises(ArticleUnavailableError):
        await fetch(REQUESTER, URL)

    assert documents.limiter.used[CLIENT_ID] == 1
    assert documents.store.rows == {}
    assert documents.storage.objects == {}


async def test_text_that_is_not_utf8_is_refused_by_the_inspector() -> None:
    documents, articles, fetch = harness()
    articles.upload = replace(articles.upload, content=b"\xff\xfe\x00")

    with pytest.raises(UnsupportedDocumentError, match="UTF-8"):
        await fetch(REQUESTER, URL)

    assert documents.store.rows == {}


async def test_text_over_the_stored_size_limit_is_refused() -> None:
    documents, articles, fetch = harness()
    articles.upload = replace(articles.upload, content=b"a" * (MAX_BYTES + 1))

    with pytest.raises(DocumentTooLargeError):
        await fetch(REQUESTER, URL)

    assert documents.store.rows == {}
