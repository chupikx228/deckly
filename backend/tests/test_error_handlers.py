from collections.abc import Callable
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from deckly.application.exceptions import RateLimitedError, UpstreamUnavailableError
from deckly.domain.exceptions import (
    DomainError,
    JobAlreadyTerminalError,
    JobNotFoundError,
    NotFoundError,
    TopicRejectedError,
)
from deckly.transport.error_handlers import register_error_handlers
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE

PROBLEM_TYPE_BASE_URL = "https://api.example.com/problems"
INTERNAL_DETAIL = "SELECT * FROM jobs WHERE api_key='sk-live-123'"


class UnmappedDomainError(DomainError):
    pass


class Payload(BaseModel):
    topic: str = Field(min_length=3)


def build_client(error_factory: Callable[[], Exception], base_url: str = PROBLEM_TYPE_BASE_URL) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, base_url)

    @app.get("/boom")
    async def boom() -> None:
        raise error_factory()

    @app.post("/payload")
    async def payload(body: Payload) -> str:
        return body.topic

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("error", "status", "code", "slug"),
    [
        (JobNotFoundError(), HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND", "job-not-found"),
        (JobAlreadyTerminalError(), HTTPStatus.CONFLICT, "JOB_ALREADY_TERMINAL", "job-already-terminal"),
        (TopicRejectedError(), HTTPStatus.UNPROCESSABLE_ENTITY, "TOPIC_REJECTED", "topic-rejected"),
        (RateLimitedError(60), HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED", "rate-limited"),
        (
            UpstreamUnavailableError(30),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "UPSTREAM_UNAVAILABLE",
            "upstream-unavailable",
        ),
    ],
)
def test_mapped_errors_become_problems(error: Exception, status: int, code: str, slug: str) -> None:
    response = build_client(lambda: error).get("/boom")

    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == f"{PROBLEM_TYPE_BASE_URL}/{slug}"
    assert body["title"]
    assert "detail" not in body


def test_retryable_error_carries_retry_after_in_body_and_header() -> None:
    response = build_client(lambda: RateLimitedError(60)).get("/boom")

    assert response.json()["retryAfterSeconds"] == 60
    assert response.headers["retry-after"] == "60"


def test_terminal_error_has_no_retry_after() -> None:
    response = build_client(JobNotFoundError).get("/boom")

    assert "retryAfterSeconds" not in response.json()
    assert "retry-after" not in response.headers


def test_domain_error_message_is_not_leaked() -> None:
    response = build_client(lambda: JobNotFoundError(INTERNAL_DETAIL)).get("/boom")

    assert INTERNAL_DETAIL not in response.text


@pytest.mark.parametrize(
    "error_factory",
    [lambda: RuntimeError(INTERNAL_DETAIL), lambda: UnmappedDomainError(INTERNAL_DETAIL), NotFoundError],
)
def test_unmapped_errors_become_internal_error_without_leaking(
    error_factory: Callable[[], Exception], caplog: pytest.LogCaptureFixture
) -> None:
    response = build_client(error_factory).get("/boom")

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "detail" not in body
    assert INTERNAL_DETAIL not in response.text
    assert "Traceback" not in response.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        ('{"topic": "ab"}', {"content-type": "application/json"}),
        ("{}", {"content-type": "application/json"}),
        ('{"topic": ', {"content-type": "application/json"}),
        ("", {}),
    ],
)
def test_invalid_request_becomes_validation_failed(content: str, headers: dict[str, str]) -> None:
    response = build_client(JobNotFoundError).post("/payload", content=content, headers=headers)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    assert response.json()["code"] == "VALIDATION_FAILED"


def test_routing_errors_keep_their_status_as_problems() -> None:
    client = build_client(JobNotFoundError)

    not_found = client.get("/missing")
    wrong_method = client.post("/boom")

    assert not_found.status_code == HTTPStatus.NOT_FOUND
    assert not_found.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    assert wrong_method.status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert wrong_method.headers["allow"] == "GET"
    assert wrong_method.json()["status"] == HTTPStatus.METHOD_NOT_ALLOWED


def test_trailing_slash_in_base_url_does_not_double_up() -> None:
    response = build_client(JobNotFoundError, f"{PROBLEM_TYPE_BASE_URL}/").get("/boom")

    assert response.json()["type"] == f"{PROBLEM_TYPE_BASE_URL}/job-not-found"


def test_subclass_of_mapped_error_inherits_its_problem() -> None:
    class ExpiredJobError(JobNotFoundError):
        pass

    response = build_client(ExpiredJobError).get("/boom")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["code"] == "JOB_NOT_FOUND"


def test_validation_detail_stays_bounded_for_many_errors() -> None:
    app = FastAPI()
    register_error_handlers(app, PROBLEM_TYPE_BASE_URL)

    class Items(BaseModel):
        items: list[int]

    @app.post("/items")
    async def items(body: Items) -> int:
        return len(body.items)

    response = TestClient(app).post("/items", json={"items": ["x"] * 5000})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert len(response.content) < 4096
