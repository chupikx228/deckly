from collections.abc import Callable
from http import HTTPStatus

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.domain.job import FailureCode, GenerationJob, JobStage, JobStatus
from deckly.main import API_PREFIX
from deckly.transport import generations
from deckly.transport.error_handlers import register_error_handlers
from deckly.transport.generations import CLIENT_ID_HEADER
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.domain.builders import JOB_ID, T0, at, basic_note, result_with
from tests.fakes import Harness
from tests.transport.openapi import declared_responses, spec_errors

PROBLEM_TYPE_BASE_URL = "https://api.example.com/problems"
CLIENT_ID = "0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"
HEADERS = {CLIENT_ID_HEADER: CLIENT_ID}
UNKNOWN_JOB_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
SPEC_PATH = "/generations/{jobId}/cancel"
SPEC_METHOD = "post"
RETRY_AFTER = "retry-after"


def queued() -> GenerationJob:
    return GenerationJob.queue(JOB_ID, T0)


def running() -> GenerationJob:
    return queued().start(at(1)).advance(JobStage.GENERATING_CARDS, 0.62, at(44))


def failed_with(code: FailureCode) -> Callable[[], GenerationJob]:
    return lambda: running().fail(code, at(60))


ACTIVE_JOBS: dict[str, Callable[[], GenerationJob]] = {"queued": queued, "running": running}
TERMINAL_JOBS: dict[str, Callable[[], GenerationJob]] = {
    "succeeded": lambda: running().succeed(result_with(basic_note(1)), at(90)),
    **{f"failed with {code}": failed_with(code) for code in FailureCode},
    "failed while queued": lambda: queued().fail(FailureCode.GENERATION_FAILED, at(5)),
    "cancelled while running": lambda: running().cancel(at(60)),
    "cancelled while queued": lambda: queued().cancel(at(5)),
}


def build_client(harness: Harness) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, PROBLEM_TYPE_BASE_URL)
    app.include_router(generations.router, prefix=API_PREFIX)
    app.state.create_generation = harness.create
    app.state.get_generation = harness.get
    app.state.cancel_generation = harness.cancel
    return TestClient(app, raise_server_exceptions=False)


def endpoint(job_id: object) -> str:
    return f"{API_PREFIX}/generations/{job_id}/cancel"


def cancel(client: TestClient, job_id: object, headers: dict[str, str] | None = None) -> httpx2.Response:
    return client.post(endpoint(job_id), headers=HEADERS if headers is None else headers)


def harness_with(job: GenerationJob) -> Harness:
    harness = Harness()
    harness.store.replace(job)
    harness.now = at(100)
    return harness


def assert_declared(response: httpx2.Response) -> None:
    assert str(response.status_code) in declared_responses(SPEC_PATH, SPEC_METHOD), response.text


def assert_no_content(response: httpx2.Response) -> None:
    assert response.status_code == HTTPStatus.NO_CONTENT, response.text
    assert_declared(response)
    assert response.content == b""
    assert "content-type" not in response.headers


def assert_problem(response: httpx2.Response, status: HTTPStatus, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == code
    assert body["status"] == status


def test_spec_declares_exactly_the_responses_this_endpoint_produces() -> None:
    responses = declared_responses(SPEC_PATH, SPEC_METHOD)

    assert set(responses) == {"204", "400", "404", "409"}
    assert responses["204"] == {"description": "Cancellation accepted"}


@pytest.mark.parametrize("make_job", ACTIVE_JOBS.values(), ids=ACTIVE_JOBS.keys())
def test_active_job_is_204_and_stored_as_cancelled(make_job: Callable[[], GenerationJob]) -> None:
    job = make_job()
    harness = harness_with(job)

    assert_no_content(cancel(build_client(harness), JOB_ID))

    stored = harness.store.jobs[JOB_ID]
    assert stored.status is JobStatus.CANCELLED
    assert (stored.stage, stored.progress, stored.updated_at) == (job.stage, job.progress, at(100))


@pytest.mark.parametrize("make_job", TERMINAL_JOBS.values(), ids=TERMINAL_JOBS.keys())
def test_terminal_job_is_409_job_already_terminal_and_left_untouched(
    make_job: Callable[[], GenerationJob],
) -> None:
    job = make_job()
    harness = harness_with(job)

    response = cancel(build_client(harness), JOB_ID)

    assert_declared(response)
    assert_problem(response, HTTPStatus.CONFLICT, "JOB_ALREADY_TERMINAL")
    assert harness.store.jobs == {JOB_ID: job}


def test_terminal_fixtures_cover_every_terminal_status() -> None:
    statuses = {make_job().status for make_job in TERMINAL_JOBS.values()}

    assert statuses == {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


def test_unknown_job_is_404_job_not_found() -> None:
    response = cancel(build_client(Harness()), UNKNOWN_JOB_ID)

    assert_declared(response)
    assert_problem(response, HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND")


MALFORMED_JOB_IDS = {
    "not a uuid": "not-a-uuid",
    "unhyphenated": JOB_ID.hex,
    "overlong": f"{JOB_ID}0",
    "non version 4": "00000000-0000-0000-0000-000000000000",
    "sql": "' OR 1=1 --",
}


@pytest.mark.parametrize("job_id", MALFORMED_JOB_IDS.values(), ids=MALFORMED_JOB_IDS.keys())
def test_malformed_job_id_is_404_even_when_its_uuid_exists(job_id: str) -> None:
    harness = harness_with(queued())

    assert_problem(cancel(build_client(harness), job_id), HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND")
    assert harness.store.jobs == {JOB_ID: queued()}


def test_uppercase_job_id_cancels_the_job() -> None:
    harness = harness_with(queued())

    assert_no_content(cancel(build_client(harness), str(JOB_ID).upper()))
    assert harness.store.jobs[JOB_ID].status is JobStatus.CANCELLED


@pytest.mark.parametrize(
    "headers",
    [{}, {CLIENT_ID_HEADER: "not-a-uuid"}, {CLIENT_ID_HEADER: CLIENT_ID.upper()}],
    ids=["missing", "not a uuid", "uppercase"],
)
def test_missing_or_malformed_client_id_is_validation_failed_and_cancels_nothing(
    headers: dict[str, str],
) -> None:
    harness = harness_with(queued())

    response = cancel(build_client(harness), JOB_ID, headers)

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")
    assert harness.store.jobs == {JOB_ID: queued()}


def test_second_cancel_is_409_and_keeps_the_first_cancellation() -> None:
    harness = harness_with(running())
    client = build_client(harness)
    assert_no_content(cancel(client, JOB_ID))
    first = harness.store.jobs[JOB_ID]
    harness.now = at(200)

    assert_problem(cancel(client, JOB_ID), HTTPStatus.CONFLICT, "JOB_ALREADY_TERMINAL")
    assert harness.store.jobs[JOB_ID] == first


def test_cancelled_job_polls_as_cancelled_without_retry_after() -> None:
    harness = harness_with(running())
    client = build_client(harness)

    assert_no_content(cancel(client, JOB_ID))
    response = client.get(f"{API_PREFIX}/generations/{JOB_ID}", headers=HEADERS)

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert spec_errors("GenerationJob", body) == []
    assert (body["status"], body["stage"], body["progress"]) == ("cancelled", "generating_cards", 0.62)
    assert (body["result"], body["error"]) == (None, None)
    assert RETRY_AFTER not in response.headers


def test_job_created_through_the_api_can_be_cancelled() -> None:
    harness = Harness()
    client = build_client(harness)
    created = client.post(
        f"{API_PREFIX}/generations",
        json={"topic": "Road signs", "language": "ru", "cardCount": 40},
        headers={**HEADERS, "Idempotency-Key": "2c9e8f7a-6b5d-4c3e-8f1a-0b9c8d7e6f5a"},
    ).json()

    assert_no_content(cancel(client, created["jobId"]))
    assert client.get(f"{API_PREFIX}/generations/{created['jobId']}", headers=HEADERS).json()["status"] == (
        "cancelled"
    )


def test_replaying_the_create_request_after_cancel_returns_the_cancelled_job_without_requeueing() -> None:
    harness = Harness()
    client = build_client(harness)
    create_headers = {**HEADERS, "Idempotency-Key": "2c9e8f7a-6b5d-4c3e-8f1a-0b9c8d7e6f5a"}
    payload = {"topic": "Road signs", "language": "ru", "cardCount": 40}
    created = client.post(f"{API_PREFIX}/generations", json=payload, headers=create_headers).json()
    assert_no_content(cancel(client, created["jobId"]))

    replay = client.post(f"{API_PREFIX}/generations", json=payload, headers=create_headers)

    assert replay.status_code == HTTPStatus.ACCEPTED
    assert spec_errors("GenerationJobCreated", replay.json()) == []
    assert (replay.json()["jobId"], replay.json()["status"]) == (created["jobId"], "cancelled")
    assert [str(job_id) for job_id in harness.queue.enqueued] == [created["jobId"]]


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
def test_wrong_method_on_the_cancel_route_is_method_not_allowed(method: str) -> None:
    response = build_client(harness_with(queued())).request(method, endpoint(JOB_ID), headers=HEADERS)

    assert_problem(response, HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")
    assert response.headers["allow"] == "POST"
