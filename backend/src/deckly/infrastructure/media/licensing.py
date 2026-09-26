import re
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

from deckly.domain.exceptions import InvariantViolationError
from deckly.domain.media import Media, MediaKind
from deckly.domain.text import is_blank, is_web_url, strip_unstorable
from deckly.infrastructure.media.client import ImageCandidate
from deckly.infrastructure.search.cleaning import ELLIPSIS, cut_at_word, visible_words

LICENSES: Mapping[str, str] = MappingProxyType({"cc0": "CC0-1.0", "pd": "Public-Domain"})
NO_ATTRIBUTION = "false"
SECURE_SCHEME = "https"
TRUSTED_DOMAIN = "wikimedia.org"
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
LABEL_SEPARATOR = "."
MIN_IMAGE_SIDE = 200
MAX_ALT_CHARACTERS = 250
DISPLAYABLE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/svg+xml", "image/webp", "image/tiff"}
)
FILE_PREFIX = "File:"
EXTENSION_SEPARATOR = "."
UNDERSCORE = "_"
WORD_SEPARATOR = " "
BREAKING_TAGS = frozenset({"br", "p", "div", "li", "ul", "ol", "tr", "td", "th", "dd", "dt"})
HIDDEN_TAGS = frozenset({"script", "style"})


class TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in HIDDEN_TAGS:
            self.hidden_depth += 1
        if tag in BREAKING_TAGS:
            self.parts.append(WORD_SEPARATOR)

    def handle_endtag(self, tag: str) -> None:
        if tag in HIDDEN_TAGS and self.hidden_depth > 0:
            self.hidden_depth -= 1
        if tag in BREAKING_TAGS:
            self.parts.append(WORD_SEPARATOR)

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(data)


def plain_text(markup: str) -> str:
    collector = TextCollector()
    collector.feed(markup)
    collector.close()
    return "".join(collector.parts)


def clean_alt(value: str) -> str:
    alt = visible_words(strip_unstorable(value))
    if is_blank(alt):
        return ""
    if len(alt) <= MAX_ALT_CHARACTERS:
        return alt
    return cut_at_word(alt, MAX_ALT_CHARACTERS - len(ELLIPSIS)) + ELLIPSIS


def title_words(file_title: str) -> str:
    name = file_title.removeprefix(FILE_PREFIX)
    stem, separator, _ = name.rpartition(EXTENSION_SEPARATOR)
    return (stem if separator else name).replace(UNDERSCORE, WORD_SEPARATOR)


def alt_text(candidate: ImageCandidate) -> str | None:
    if candidate.description is not None:
        described = clean_alt(plain_text(candidate.description))
        if described:
            return described
    return clean_alt(title_words(candidate.file_title)) or None


def is_plain_host(host: str) -> bool:
    return all(HOST_LABEL.fullmatch(label) for label in host.split(LABEL_SEPARATOR))


def is_trusted_host(host: str) -> bool:
    return is_plain_host(host) and (
        host == TRUSTED_DOMAIN or host.endswith(f"{LABEL_SEPARATOR}{TRUSTED_DOMAIN}")
    )


def is_trusted_url(url: str) -> bool:
    if not is_web_url(url):
        return False
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == SECURE_SCHEME
        and is_trusted_host(parts.hostname or "")
        and parts.username is None
        and parts.password is None
        and port is None
    )


def licence_of(candidate: ImageCandidate) -> str | None:
    code = (candidate.license_code or "").strip().lower()
    attribution = (candidate.attribution_required or "").strip().lower()
    if attribution != NO_ATTRIBUTION:
        return None
    if candidate.restrictions is not None and not is_blank(candidate.restrictions):
        return None
    return LICENSES.get(code)


def has_usable_picture(candidate: ImageCandidate) -> bool:
    width, height = candidate.thumbnail_width, candidate.thumbnail_height
    return (
        candidate.mime in DISPLAYABLE_MIME_TYPES
        and candidate.thumbnail_url is not None
        and is_trusted_url(candidate.thumbnail_url)
        and width is not None
        and height is not None
        and min(width, height) >= MIN_IMAGE_SIDE
    )


def licensed_image(candidate: ImageCandidate, new_id: Callable[[], UUID]) -> Media | None:
    licence = licence_of(candidate)
    if licence is None or candidate.thumbnail_url is None or not has_usable_picture(candidate):
        return None
    alt = alt_text(candidate)
    if alt is None:
        return None
    try:
        return Media(
            media_id=new_id(),
            kind=MediaKind.IMAGE,
            url=candidate.thumbnail_url,
            license=licence,
            alt=alt,
            width=candidate.thumbnail_width,
            height=candidate.thumbnail_height,
        )
    except InvariantViolationError:
        return None
