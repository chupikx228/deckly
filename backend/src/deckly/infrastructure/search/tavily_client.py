import httpx2

from deckly.infrastructure.resilience import RETRY_AFTER_HEADER, is_transient_status, parse_retry_after
from deckly.infrastructure.search.client import (
    SearchEndpoint,
    SearchError,
    SearchHit,
    SearchQuery,
    SearchQuotaExhaustedError,
    SearchRejectedError,
    SearchResponseError,
    SearchUnavailableError,
)

PROVIDER = "Tavily"
SEARCH_PATH = "/search"
AUTHORIZATION_HEADER = "Authorization"
GENERAL_TOPIC = "general"
SEARCH_DEPTH = "basic"
RAW_CONTENT_FORMAT = "text"
PLAN_LIMIT_EXCEEDED = 432
PAY_AS_YOU_GO_LIMIT_EXCEEDED = 433
QUOTA_STATUSES = frozenset({PLAN_LIMIT_EXCEEDED, PAY_AS_YOU_GO_LIMIT_EXCEEDED})
MAX_DETAIL_LENGTH = 500
REDACTED = "[redacted]"


def request_body(query: SearchQuery) -> dict[str, object]:
    body: dict[str, object] = {
        "query": query.text,
        "topic": GENERAL_TOPIC,
        "search_depth": SEARCH_DEPTH,
        "max_results": query.max_results,
        "include_raw_content": RAW_CONTENT_FORMAT,
        "include_answer": False,
        "include_images": False,
    }
    if query.language is not None:
        body["language"] = query.language
    return body


def page_text(raw_content: object, snippet: object) -> str:
    if isinstance(raw_content, str) and raw_content.strip():
        return raw_content
    return snippet if isinstance(snippet, str) else ""


def hit_from(result: object) -> SearchHit | None:
    match result:
        case {"title": str() as title, "url": str() as url, **fields}:
            return SearchHit(
                title=title, url=url, content=page_text(fields.get("raw_content"), fields.get("content"))
            )
    return None


def hits_from(response: httpx2.Response) -> tuple[SearchHit, ...]:
    try:
        payload: object = response.json()
    except ValueError as error:
        message = f"{PROVIDER} answered with a body that is not JSON"
        raise SearchResponseError(message) from error
    match payload:
        case {"results": list() as results}:
            return tuple(hit for hit in map(hit_from, results) if hit is not None)
    message = f"{PROVIDER} answered without a results list"
    raise SearchResponseError(message)


def error_detail(response: httpx2.Response, api_key: str) -> str:
    try:
        payload: object = response.json()
    except ValueError:
        return ""
    match payload:
        case {"detail": {"error": str() as detail}} | {"detail": str() as detail}:
            redacted = detail.replace(api_key, REDACTED) if api_key else detail
            return redacted[:MAX_DETAIL_LENGTH]
    return ""


def status_error(response: httpx2.Response, api_key: str) -> SearchError:
    status = response.status_code
    message = f"{PROVIDER} answered {status}: {error_detail(response, api_key)}"
    if status in QUOTA_STATUSES:
        return SearchQuotaExhaustedError(message)
    if is_transient_status(status):
        retry_after = parse_retry_after(response.headers.get(RETRY_AFTER_HEADER))
        return SearchUnavailableError(message, retry_after_seconds=retry_after)
    return SearchRejectedError(message)


class TavilySearchClient:
    def __init__(self, endpoint: SearchEndpoint, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._api_key = endpoint.api_key
        self._client = httpx2.AsyncClient(
            base_url=endpoint.base_url,
            headers={AUTHORIZATION_HEADER: f"Bearer {endpoint.api_key}"},
            timeout=endpoint.timeout_seconds,
            transport=transport,
        )

    async def search(self, query: SearchQuery) -> tuple[SearchHit, ...]:
        try:
            response = await self._client.post(SEARCH_PATH, json=request_body(query))
        except httpx2.TransportError as error:
            message = f"{PROVIDER} could not be reached: {type(error).__name__}"
            raise SearchUnavailableError(message) from error
        except httpx2.HTTPError as error:
            message = f"{PROVIDER} request failed: {type(error).__name__}"
            raise SearchResponseError(message) from error
        if not response.is_success:
            raise status_error(response, self._api_key)
        return hits_from(response)

    async def aclose(self) -> None:
        await self._client.aclose()
