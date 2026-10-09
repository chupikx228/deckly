from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from deckly.domain.exceptions import InvalidMediaError
from deckly.domain.text import is_blank, is_uuid_v4, is_web_url


class MediaKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class Attribution:
    author: str
    title: str
    source_url: str
    license_url: str

    def __post_init__(self) -> None:
        for name, text in (("author", self.author), ("title", self.title)):
            if is_blank(text):
                message = f"attribution {name} must not be blank"
                raise InvalidMediaError(message)
        for name, url in (("source url", self.source_url), ("licence url", self.license_url)):
            if not is_web_url(url):
                message = f"attribution {name} {url!r} is not an http(s) url"
                raise InvalidMediaError(message)


@dataclass(frozen=True, slots=True)
class Media:
    media_id: UUID
    kind: MediaKind
    url: str
    license: str
    alt: str | None = None
    width: int | None = None
    height: int | None = None
    attribution: Attribution | None = None

    def __post_init__(self) -> None:
        if not is_uuid_v4(self.media_id):
            message = f"mediaId {self.media_id} is not a UUID v4"
            raise InvalidMediaError(message)
        if not is_web_url(self.url):
            message = f"media url {self.url!r} is not an http(s) url"
            raise InvalidMediaError(message)
        if is_blank(self.license):
            message = "media without a known licence must not be returned"
            raise InvalidMediaError(message)
        if self.kind is MediaKind.IMAGE and (self.alt is None or is_blank(self.alt)):
            message = "images must carry alt text"
            raise InvalidMediaError(message)
        for name, dimension in (("width", self.width), ("height", self.height)):
            if dimension is not None and dimension <= 0:
                message = f"media {name} must be positive, got {dimension}"
                raise InvalidMediaError(message)
