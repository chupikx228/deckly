import logging
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from deckly.application.exceptions import (
    ApplicationError,
    RateLimitedError,
    RetryableError,
    UpstreamUnavailableError,
)
from deckly.domain.exceptions import (
    DomainError,
    JobAlreadyTerminalError,
    JobNotFoundError,
    TopicRejectedError,
)
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE, ErrorCode, Problem

RETRY_AFTER_HEADER = "Retry-After"
MAX_DESCRIBED_VALIDATION_ERRORS = 10

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProblemKind:
    status: HTTPStatus
    code: ErrorCode
    title: str


VALIDATION_FAILED = ProblemKind(HTTPStatus.BAD_REQUEST, ErrorCode.VALIDATION_FAILED, "Validation failed")
INTERNAL_ERROR = ProblemKind(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL_ERROR, "Internal error")

PROBLEM_KINDS: Mapping[type[Exception], ProblemKind] = {
    JobNotFoundError: ProblemKind(HTTPStatus.NOT_FOUND, ErrorCode.JOB_NOT_FOUND, "Job not found"),
    JobAlreadyTerminalError: ProblemKind(
        HTTPStatus.CONFLICT, ErrorCode.JOB_ALREADY_TERMINAL, "Job already terminal"
    ),
    TopicRejectedError: ProblemKind(
        HTTPStatus.UNPROCESSABLE_ENTITY, ErrorCode.TOPIC_REJECTED, "Topic rejected"
    ),
    RateLimitedError: ProblemKind(HTTPStatus.TOO_MANY_REQUESTS, ErrorCode.RATE_LIMITED, "Rate limited"),
    UpstreamUnavailableError: ProblemKind(
        HTTPStatus.SERVICE_UNAVAILABLE, ErrorCode.UPSTREAM_UNAVAILABLE, "Upstream unavailable"
    ),
}


def find_problem_kind(exc: Exception) -> ProblemKind | None:
    for cls in type(exc).__mro__:
        kind = PROBLEM_KINDS.get(cls)
        if kind is not None:
            return kind
    return None


def describe_validation_errors(exc: RequestValidationError) -> str:
    errors = exc.errors()
    described = [describe_validation_error(error) for error in errors[:MAX_DESCRIBED_VALIDATION_ERRORS]]
    omitted = len(errors) - len(described)
    if omitted > 0:
        described.append(f"{omitted} more errors omitted")
    return "; ".join(described)


def describe_validation_error(error: Mapping[str, object]) -> str:
    location = error.get("loc")
    path = ".".join(str(part) for part in location) if isinstance(location, tuple | list) else ""
    return f"{path}: {error.get('msg')}"


class ProblemResponder:
    def __init__(self, problem_type_base_url: str) -> None:
        self._problem_type_base_url = problem_type_base_url.rstrip("/")

    def respond(
        self,
        kind: ProblemKind,
        *,
        status: int | None = None,
        detail: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> JSONResponse:
        problem = Problem(
            type=self._type_url(kind.code),
            title=kind.title,
            status=status or kind.status,
            code=kind.code,
            detail=detail,
            retry_after_seconds=retry_after_seconds,
        )
        headers = {} if retry_after_seconds is None else {RETRY_AFTER_HEADER: str(retry_after_seconds)}
        return JSONResponse(
            content=problem.model_dump(mode="json", by_alias=True, exclude_none=True),
            status_code=problem.status,
            media_type=PROBLEM_JSON_MEDIA_TYPE,
            headers=headers,
        )

    async def handle_known_error(self, request: Request, exc: Exception) -> JSONResponse:
        kind = find_problem_kind(exc)
        if kind is None:
            return await self.handle_unexpected_error(request, exc)
        retry_after_seconds = exc.retry_after_seconds if isinstance(exc, RetryableError) else None
        return self.respond(kind, retry_after_seconds=retry_after_seconds)

    async def handle_validation_error(self, _request: Request, exc: RequestValidationError) -> JSONResponse:
        return self.respond(VALIDATION_FAILED, detail=describe_validation_errors(exc))

    async def handle_http_error(self, _request: Request, exc: HTTPException) -> JSONResponse:
        response = self.respond(INTERNAL_ERROR, status=exc.status_code)
        response.headers.update(exc.headers or {})
        return response

    async def handle_unexpected_error(self, request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            exc_info=exc,
            extra={"method": request.method, "path": request.url.path},
        )
        return self.respond(INTERNAL_ERROR)

    def _type_url(self, code: ErrorCode) -> str:
        return f"{self._problem_type_base_url}/{code.lower().replace('_', '-')}"


def register_error_handlers(app: FastAPI, problem_type_base_url: str) -> None:
    responder = ProblemResponder(problem_type_base_url)
    app.exception_handler(DomainError)(responder.handle_known_error)
    app.exception_handler(ApplicationError)(responder.handle_known_error)
    app.exception_handler(RequestValidationError)(responder.handle_validation_error)
    app.exception_handler(HTTPException)(responder.handle_http_error)
    app.exception_handler(Exception)(responder.handle_unexpected_error)
