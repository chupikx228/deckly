from dataclasses import dataclass
from http import HTTPStatus

from protego import Protego

from deckly.application.exceptions import UpstreamUnavailableError

ROBOTS_PRODUCT = "Deckly"
ROBOTS_PATH = "/robots.txt"
MAX_ROBOTS_BYTES = 500 * 1024
MAX_ROBOTS_REDIRECTS = 5
ROBOTS_RETRY_AFTER_SECONDS = 60
ROBOTS_ENCODING = "utf-8-sig"


class RobotsUnreachableError(UpstreamUnavailableError):
    pass


@dataclass(frozen=True, slots=True)
class RobotsRules:
    parsed: Protego | None = None

    def allows(self, url: str) -> bool:
        return self.parsed is None or self.parsed.can_fetch(url, ROBOTS_PRODUCT)


ALLOW_ALL = RobotsRules()


def is_unreachable(status: int) -> bool:
    return status >= HTTPStatus.INTERNAL_SERVER_ERROR


def is_success(status: int) -> bool:
    return HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES


def robots_rules(status: int, body: bytes) -> RobotsRules:
    if is_success(status):
        return RobotsRules(Protego.parse(body[:MAX_ROBOTS_BYTES].decode(ROBOTS_ENCODING, errors="replace")))
    if not is_unreachable(status):
        return ALLOW_ALL
    error = RobotsUnreachableError(ROBOTS_RETRY_AFTER_SECONDS)
    error.add_note(f"robots.txt answered HTTP {status}, so the whole site is treated as disallowed")
    raise error
