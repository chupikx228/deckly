import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import RetrievedPage
from deckly.domain.exceptions import InvalidSourceError
from deckly.domain.generation import GenerationRequest
from deckly.domain.source import Source
from deckly.domain.text import strip_unstorable
from deckly.infrastructure.resilience import MIN_RETRY_AFTER_SECONDS, CallSize, ResilientCaller
from deckly.infrastructure.search.cleaning import clean_title, visible_words
from deckly.infrastructure.search.client import (
    SearchClient,
    SearchHit,
    SearchQuery,
    SearchQuotaExhaustedError,
)

logger = logging.getLogger(__name__)

LANGUAGE_SUBTAG_SEPARATOR = "-"
LANGUAGE_CODE_LENGTH = 2


def language_hint(tag: str) -> str | None:
    primary = tag.partition(LANGUAGE_SUBTAG_SEPARATOR)[0].lower()
    if len(primary) == LANGUAGE_CODE_LENGTH and primary.isascii() and primary.isalpha():
        return primary
    return None


def page_from(hit: SearchHit, retrieved_at: datetime) -> RetrievedPage | None:
    try:
        source = Source(title=clean_title(hit.title), url=hit.url, retrieved_at=retrieved_at)
    except InvalidSourceError:
        return None
    return RetrievedPage(source=source, content=hit.content)


def unique_pages(hits: Iterable[SearchHit], retrieved_at: datetime) -> tuple[RetrievedPage, ...]:
    pages: dict[str, RetrievedPage] = {}
    for hit in hits:
        page = page_from(hit, retrieved_at)
        if page is not None and page.source.url not in pages:
            pages[page.source.url] = page
    return tuple(pages.values())


@dataclass(frozen=True, slots=True)
class WebSourceRetriever:
    client: SearchClient
    caller: ResilientCaller
    clock: Callable[[], datetime]
    max_results: int

    async def retrieve(self, job_id: UUID, request: GenerationRequest) -> tuple[RetrievedPage, ...]:
        query = SearchQuery(
            text=visible_words(strip_unstorable(request.topic)),
            language=language_hint(request.language),
            max_results=self.max_results,
        )
        try:
            hits = await self.caller.call(lambda: self.client.search(query), size=CallSize.TYPICAL)
        except SearchQuotaExhaustedError as error:
            raise UpstreamUnavailableError(MIN_RETRY_AFTER_SECONDS) from error
        pages = unique_pages(hits, self.clock())[: self.max_results]
        logger.log(
            logging.INFO if pages else logging.WARNING,
            "sources_retrieved",
            extra={"job_id": str(job_id), "hits": len(hits), "pages": len(pages)},
        )
        return pages

    async def aclose(self) -> None:
        await self.client.aclose()
