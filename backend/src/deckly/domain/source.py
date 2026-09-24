from dataclasses import dataclass
from datetime import datetime

from deckly.domain.exceptions import InvalidSourceError
from deckly.domain.text import is_blank, is_timezone_aware, is_web_url


@dataclass(frozen=True, slots=True)
class Source:
    title: str
    url: str
    retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        if is_blank(self.title):
            message = "source title must not be blank"
            raise InvalidSourceError(message)
        if not is_web_url(self.url):
            message = f"source url {self.url!r} is not an http(s) url"
            raise InvalidSourceError(message)
        if self.retrieved_at is not None and not is_timezone_aware(self.retrieved_at):
            message = "source retrievedAt must carry an explicit offset"
            raise InvalidSourceError(message)
