import logging
import time
from collections.abc import Iterable
from contextlib import ExitStack
from http import HTTPStatus
from uuid import UUID

from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from deckly.application.correlation import CLIENT_ID, correlated

logger = logging.getLogger(__name__)

CLIENT_ID_HEADER = b"x-client-id"
UUID_VERSION = 4
UNMATCHED_ROUTE = "unmatched"
OTHER_METHOD = "_OTHER"
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"})
MILLISECONDS_PER_SECOND = 1000
METHOD_ATTRIBUTE = "http.request.method"
ROUTE_ATTRIBUTE = "http.route"
STATUS_ATTRIBUTE = "http.response.status_code"


def client_id_from(headers: Iterable[tuple[bytes, bytes]]) -> UUID | None:
    value = next((value for name, value in headers if name.lower() == CLIENT_ID_HEADER), None)
    if value is None:
        return None
    try:
        text = value.decode("ascii")
        client_id = UUID(text)
    except (UnicodeDecodeError, ValueError):
        return None
    return client_id if str(client_id) == text and client_id.version == UUID_VERSION else None


def known_method(method: object) -> str:
    return method if isinstance(method, str) and method in KNOWN_METHODS else OTHER_METHOD


def route_of(scope: Scope) -> str:
    template: object = getattr(scope.get("route"), "path_format", None)
    if not isinstance(template, str):
        return UNMATCHED_ROUTE
    params: object = scope.get("path_params")
    try:
        concrete = template.format(**params) if isinstance(params, dict) else template
    except (KeyError, IndexError, ValueError):
        return template
    path = str(scope.get("path", ""))
    if not path.endswith(concrete):
        return template
    return f"{path.removesuffix(concrete)}{template}"


class ObservedRequests:
    def __init__(self, app: ASGIApp, *, tracer: Tracer, quiet_paths: frozenset[str]) -> None:
        self._app = app
        self._tracer = tracer
        self._quiet_paths = quiet_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        method = known_method(scope["method"])
        status = HTTPStatus.INTERNAL_SERVER_ERROR.value

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        started = time.perf_counter()
        with ExitStack() as stack:
            client_id = client_id_from(scope["headers"])
            if client_id is not None:
                stack.enter_context(correlated(CLIENT_ID, client_id))
            span = stack.enter_context(
                self._tracer.start_as_current_span(
                    f"{method} request",
                    kind=SpanKind.SERVER,
                    attributes={METHOD_ATTRIBUTE: method},
                    record_exception=False,
                    set_status_on_exception=False,
                )
            )
            try:
                await self._app(scope, receive, observed_send)
            finally:
                route = route_of(scope)
                span.update_name(f"{method} {route}")
                span.set_attribute(ROUTE_ATTRIBUTE, route)
                span.set_attribute(STATUS_ATTRIBUTE, status)
                if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                    span.set_status(Status(StatusCode.ERROR))
                if scope["path"] not in self._quiet_paths:
                    logger.info(
                        "http_request_finished",
                        extra={
                            "method": method,
                            "route": route,
                            "status": status,
                            "duration_ms": round((time.perf_counter() - started) * MILLISECONDS_PER_SECOND),
                        },
                    )
