from dataclasses import dataclass
from typing import Protocol

from deckly.infrastructure.resilience import PersistentError, TransientError


@dataclass(frozen=True, slots=True)
class SearchEndpoint:
    base_url: str
    api_key: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    language: str | None
    max_results: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    content: str


class SearchClient(Protocol):
    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]: ...

    async def aclose(self) -> None: ...


class SearchError(Exception):
    pass


class SearchUnavailableError(SearchError, TransientError):
    pass


class SearchQuotaExhaustedError(SearchError, PersistentError):
    pass


class SearchRejectedError(SearchError):
    pass


class SearchResponseError(SearchError):
    pass
