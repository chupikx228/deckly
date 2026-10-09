import unicodedata
from collections.abc import Mapping
from http import HTTPStatus

import httpx2

from deckly.infrastructure.media.client import (
    ImageCandidate,
    ImageSearch,
    MediaBlockedError,
    MediaEndpoint,
    MediaError,
    MediaRejectedError,
    MediaResponseError,
    MediaUnavailableError,
)
from deckly.infrastructure.resilience import RETRY_AFTER_HEADER, is_transient_status, parse_retry_after
from deckly.infrastructure.search.cleaning import cut_at_word

PROVIDER = "Wikimedia Commons"
API_PATH = "/w/api.php"
USER_AGENT_HEADER = "User-Agent"
FILE_NAMESPACE = 6
FILE_TYPES = "filetype:bitmap|drawing"
PAGE_PROPERTIES = "imageinfo|categories"
IMAGE_PROPERTIES = "url|size|mime|extmetadata"
VISIBLE_CATEGORIES = "!hidden"
ALL_CATEGORIES = "max"
CATEGORY_PREFIX = "Category:"
LICENSE_FIELD = "License"
ATTRIBUTION_REQUIRED_FIELD = "AttributionRequired"
RESTRICTIONS_FIELD = "Restrictions"
DESCRIPTION_FIELD = "ImageDescription"
ARTIST_FIELD = "Artist"
CREDIT_LINE_FIELD = "Attribution"
METADATA_FILTER = (
    f"{LICENSE_FIELD}|{ATTRIBUTION_REQUIRED_FIELD}|{RESTRICTIONS_FIELD}|{DESCRIPTION_FIELD}|"
    f"{ARTIST_FIELD}|{CREDIT_LINE_FIELD}"
)
TRANSIENT_API_ERRORS = frozenset({"maxlag", "ratelimited", "readonly"})
INTERNAL_API_ERROR_PREFIX = "internal_api_error"
SEARCH_TERM_CATEGORIES = frozenset({"L", "M", "N"})
WORD_SEPARATOR = " "
MAX_SEARCH_CHARACTERS = 300
MAX_DETAIL_LENGTH = 500

type RankedCandidate = tuple[int, ImageCandidate]


def is_search_term_character(character: str) -> bool:
    return unicodedata.category(character)[0] in SEARCH_TERM_CATEGORIES


def search_terms(text: str) -> str:
    kept = "".join(character if is_search_term_character(character) else WORD_SEPARATOR for character in text)
    return cut_at_word(WORD_SEPARATOR.join(kept.lower().split()), MAX_SEARCH_CHARACTERS)


def search_params(search: ImageSearch, terms: str) -> dict[str, str | int]:
    return {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrsearch": f"{terms} {FILE_TYPES}",
        "gsrnamespace": FILE_NAMESPACE,
        "gsrlimit": search.max_candidates,
        "prop": PAGE_PROPERTIES,
        "iiprop": IMAGE_PROPERTIES,
        "iiurlwidth": search.thumbnail_width,
        "iiextmetadatafilter": METADATA_FILTER,
        "clshow": VISIBLE_CATEGORIES,
        "cllimit": ALL_CATEGORIES,
    }


def text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def positive_or_none(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def metadata_value(metadata: Mapping[str, object], key: str) -> str | None:
    match metadata.get(key):
        case {"value": str() as value}:
            return value
    return None


def category_name(entry: object) -> str | None:
    match entry:
        case {"title": str() as title} if title.startswith(CATEGORY_PREFIX):
            return title.removeprefix(CATEGORY_PREFIX)
    return None


def categories_of(entries: object) -> tuple[str, ...]:
    if not isinstance(entries, list):
        return ()
    return tuple(name for name in map(category_name, entries) if name is not None)


def candidate_from(page: object) -> RankedCandidate | None:
    match page:
        case {"title": str() as title, "index": int() as index, "imageinfo": [{**info}, *_], **rest}:
            extmetadata = info.get("extmetadata")
            metadata: Mapping[str, object] = extmetadata if isinstance(extmetadata, dict) else {}
            return index, ImageCandidate(
                file_title=title,
                mime=text_or_none(info.get("mime")),
                thumbnail_url=text_or_none(info.get("thumburl")),
                thumbnail_width=positive_or_none(info.get("thumbwidth")),
                thumbnail_height=positive_or_none(info.get("thumbheight")),
                license_code=metadata_value(metadata, LICENSE_FIELD),
                attribution_required=metadata_value(metadata, ATTRIBUTION_REQUIRED_FIELD),
                restrictions=metadata_value(metadata, RESTRICTIONS_FIELD),
                description=metadata_value(metadata, DESCRIPTION_FIELD),
                description_url=text_or_none(info.get("descriptionurl")),
                artist=metadata_value(metadata, ARTIST_FIELD),
                credit_line=metadata_value(metadata, CREDIT_LINE_FIELD),
                categories=categories_of(rest.get("categories")),
            )
    return None


def ranked_candidates(pages: list[object]) -> tuple[ImageCandidate, ...]:
    ranked = [candidate for candidate in map(candidate_from, pages) if candidate is not None]
    ranked.sort(key=lambda entry: entry[0])
    return tuple(candidate for _, candidate in ranked)


def api_error(code: str, info: object, response: httpx2.Response) -> MediaError:
    detail = info[:MAX_DETAIL_LENGTH] if isinstance(info, str) else ""
    message = f"{PROVIDER} answered with error {code}: {detail}"
    if code in TRANSIENT_API_ERRORS or code.startswith(INTERNAL_API_ERROR_PREFIX):
        retry_after = parse_retry_after(response.headers.get(RETRY_AFTER_HEADER))
        return MediaUnavailableError(message, retry_after_seconds=retry_after)
    return MediaRejectedError(message)


def candidates_from(response: httpx2.Response) -> tuple[ImageCandidate, ...]:
    try:
        payload: object = response.json()
    except ValueError as error:
        message = f"{PROVIDER} answered with a body that is not JSON"
        raise MediaResponseError(message) from error
    match payload:
        case {"error": {"code": str() as code, **details}}:
            raise api_error(code, details.get("info"), response)
        case {"query": {"pages": list() as pages}}:
            return ranked_candidates(pages)
        case {"batchcomplete": True, **rest} if "query" not in rest:
            return ()
    message = f"{PROVIDER} answered without a page list"
    raise MediaResponseError(message)


def status_error(response: httpx2.Response) -> MediaError:
    status = response.status_code
    message = f"{PROVIDER} answered {status}"
    if status == HTTPStatus.FORBIDDEN:
        return MediaBlockedError(message)
    if is_transient_status(status):
        retry_after = parse_retry_after(response.headers.get(RETRY_AFTER_HEADER))
        return MediaUnavailableError(message, retry_after_seconds=retry_after)
    return MediaRejectedError(message)


class CommonsImageSearchClient:
    def __init__(self, endpoint: MediaEndpoint, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._client = httpx2.AsyncClient(
            base_url=endpoint.base_url,
            headers={USER_AGENT_HEADER: endpoint.user_agent},
            timeout=endpoint.timeout_seconds,
            transport=transport,
        )

    async def search(self, search: ImageSearch) -> tuple[ImageCandidate, ...]:
        terms = search_terms(search.text)
        if not terms:
            return ()
        try:
            response = await self._client.get(API_PATH, params=search_params(search, terms))
        except httpx2.TransportError as error:
            message = f"{PROVIDER} could not be reached: {type(error).__name__}"
            raise MediaUnavailableError(message) from error
        except httpx2.HTTPError as error:
            message = f"{PROVIDER} request failed: {type(error).__name__}"
            raise MediaResponseError(message) from error
        if not response.is_success:
            raise status_error(response)
        return candidates_from(response)

    async def aclose(self) -> None:
        await self._client.aclose()
