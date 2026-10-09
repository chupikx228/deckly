from dataclasses import dataclass
from typing import Protocol

from deckly.infrastructure.resilience import PersistentError, TransientError


@dataclass(frozen=True, slots=True)
class MediaEndpoint:
    base_url: str
    user_agent: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ImageSearch:
    text: str
    max_candidates: int
    thumbnail_width: int


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    file_title: str
    mime: str | None
    thumbnail_url: str | None
    thumbnail_width: int | None
    thumbnail_height: int | None
    license_code: str | None
    attribution_required: str | None
    restrictions: str | None
    description: str | None
    description_url: str | None = None
    artist: str | None = None
    credit_line: str | None = None
    categories: tuple[str, ...] = ()


class ImageSearchClient(Protocol):
    async def search(self, search: ImageSearch) -> tuple[ImageCandidate, ...]: ...

    async def aclose(self) -> None: ...


class MediaError(Exception):
    pass


class MediaUnavailableError(MediaError, TransientError):
    pass


class MediaBlockedError(MediaError, PersistentError):
    pass


class MediaRejectedError(MediaError):
    pass


class MediaResponseError(MediaError):
    pass
