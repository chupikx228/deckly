from collections.abc import Callable
from datetime import timedelta
from http import HTTPStatus
from itertools import accumulate

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.domain.deck import Deck, GenerationResult
from deckly.domain.job import STAGE_ORDER, FailureCode, GenerationJob, JobStage, Progress, Running
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE
from deckly.main import API_PREFIX
from deckly.transport import generations
from deckly.transport.error_handlers import register_error_handlers
from deckly.transport.generations import CLIENT_ID_HEADER, POLL_RETRY_AFTER_SECONDS
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.domain.builders import (
    AUDIO,
    FIELDS_BY_TYPE,
    FULL_RESULT,
    IMAGE_ID,
    JOB_ID,
    MOSCOW,
    T0,
    at,
)
from tests.fakes import Harness
from tests.transport.openapi import spec_errors

PROBLEM_TYPE_BASE_URL = "https://api.example.com/problems"
CLIENT_ID = "0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"
HEADERS = {CLIENT_ID_HEADER: CLIENT_ID}
UNKNOWN_JOB_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
JOB_FIELDS = {"jobId", "status", "stage", "progress", "createdAt", "updatedAt", "result", "error"}
RETRY_AFTER = "retry-after"

WIRE_FIELDS_BY_TYPE: dict[NoteType, set[str]] = {
    NoteType.BASIC: {"front", "back"},
    NoteType.BASIC_REVERSED: {"front", "back"},
    NoteType.BASIC_OPTIONAL_REVERSED: {"front", "back", "addReverse"},
    NoteType.BASIC_TYPE_IN: {"front", "back"},
    NoteType.CLOZE: {"text", "extra"},
    NoteType.MULTIPLE_CHOICE: {"question", "answer", "distractors"},
    NoteType.IMAGE_OCCLUSION: {"imageId", "regions", "extra"},
}


BARE_RESULT = GenerationResult(deck=Deck(title="Road signs"), notes=())


def queued() -> GenerationJob:
    return GenerationJob.queue(JOB_ID, T0)


def running() -> GenerationJob:
    return queued().start(at(1)).advance(JobStage.GENERATING_CARDS, 0.62, at(44))


def succeeded(result: GenerationResult = FULL_RESULT) -> GenerationJob:
    return running().succeed(result, at(90))


def failed_with(code: FailureCode) -> Callable[[], GenerationJob]:
    return lambda: running().fail(code, at(60))


JOBS: dict[str, Callable[[], GenerationJob]] = {
    "queued": queued,
    "running": running,
    "succeeded": succeeded,
    "succeeded with a bare deck and no notes": lambda: succeeded(BARE_RESULT),
    **{f"failed with {code}": failed_with(code) for code in FailureCode},
    "failed while queued": lambda: queued().fail(FailureCode.GENERATION_FAILED, at(5)),
    "cancelled while running": lambda: running().cancel(at(60)),
    "cancelled while queued": lambda: queued().cancel(at(5)),
}
NON_TERMINAL = {"queued", "running"}


def build_client(harness: Harness) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, PROBLEM_TYPE_BASE_URL)
    app.include_router(generations.router, prefix=API_PREFIX)
    app.state.create_generation = harness.create
    app.state.get_generation = harness.get
    return TestClient(app, raise_server_exceptions=False)


def endpoint(job_id: object) -> str:
    return f"{API_PREFIX}/generations/{job_id}"


def poll(client: TestClient, job_id: object, headers: dict[str, str] | None = None) -> httpx2.Response:
    return client.get(endpoint(job_id), headers=HEADERS if headers is None else headers)


def poll_stored(job: GenerationJob) -> httpx2.Response:
    harness = Harness()
    harness.store.replace(job)
    return poll(build_client(harness), job.job_id)


def assert_polled(response: httpx2.Response) -> dict[str, object]:
    assert response.status_code == HTTPStatus.OK, response.text
    assert response.headers["content-type"] == "application/json"
    body: dict[str, object] = response.json()
    assert spec_errors("GenerationJob", body) == []
    assert set(body) == JOB_FIELDS
    return body


def assert_problem(response: httpx2.Response, status: HTTPStatus, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == code


@pytest.mark.parametrize("make_job", JOBS.values(), ids=JOBS.keys())
def test_every_state_is_200_and_validates_against_the_spec(make_job: Callable[[], GenerationJob]) -> None:
    job = make_job()

    body = assert_polled(poll_stored(job))

    assert body["jobId"] == str(job.job_id)
    assert body["status"] == job.status
    assert body["stage"] == job.stage
    assert body["progress"] == job.progress.value


@pytest.mark.parametrize("make_job", JOBS.values(), ids=JOBS.keys())
def test_result_is_present_only_when_succeeded_and_error_only_when_failed(
    make_job: Callable[[], GenerationJob],
) -> None:
    job = make_job()

    body = assert_polled(poll_stored(job))

    assert (body["result"] is not None) == (job.status == "succeeded")
    assert (body["error"] is not None) == (job.status == "failed")


@pytest.mark.parametrize("make_job", JOBS.values(), ids=JOBS.keys())
def test_retry_after_is_sent_only_while_non_terminal(make_job: Callable[[], GenerationJob]) -> None:
    job = make_job()

    response = poll_stored(job)

    if job.is_terminal:
        assert RETRY_AFTER not in response.headers
    else:
        assert response.headers[RETRY_AFTER] == str(POLL_RETRY_AFTER_SECONDS)


def test_non_terminal_fixtures_cover_every_non_terminal_status() -> None:
    assert {name for name, make_job in JOBS.items() if not make_job().is_terminal} == NON_TERMINAL


@pytest.mark.parametrize("code", list(FailureCode))
def test_failed_generation_is_200_with_its_failure_code_not_a_server_error(code: FailureCode) -> None:
    response = poll_stored(running().fail(code, at(60)))

    body = assert_polled(response)
    assert body["status"] == "failed"
    error = body["error"]
    assert isinstance(error, dict)
    assert spec_errors("Problem", error) == []
    assert error["code"] == code
    assert error["status"] == HTTPStatus.OK
    assert error["type"] == f"{PROBLEM_TYPE_BASE_URL}/{code.lower().replace('_', '-')}"
    assert "detail" not in error
    assert "retryAfterSeconds" not in error


def test_failed_job_keeps_the_stage_and_progress_it_stopped_at() -> None:
    body = assert_polled(poll_stored(running().fail(FailureCode.PROVIDER_UNAVAILABLE, at(60))))

    assert (body["stage"], body["progress"]) == ("generating_cards", 0.62)


def test_succeeded_job_reports_complete_progress_and_no_stage() -> None:
    body = assert_polled(poll_stored(succeeded()))

    assert (body["stage"], body["progress"]) == (None, 1.0)


def succeeded_notes() -> list[dict[str, object]]:
    result = assert_polled(poll_stored(succeeded()))["result"]
    assert isinstance(result, dict)
    notes = result["notes"]
    assert isinstance(notes, list)
    return notes


def fields_by_note_type() -> dict[object, dict[str, object]]:
    shapes: dict[object, dict[str, object]] = {}
    for note in succeeded_notes():
        fields = note["fields"]
        assert isinstance(fields, dict)
        shapes[note["noteType"]] = fields
    return shapes


def test_every_note_in_the_result_validates_against_the_spec() -> None:
    notes = succeeded_notes()

    assert len(notes) == len(FULL_RESULT.notes)
    for note in notes:
        assert spec_errors("GeneratedNote", note) == []


def test_fields_of_every_registered_note_type_have_their_contract_shape() -> None:
    assert set(FIELDS_BY_TYPE) == set(NOTE_FIELDS_BY_TYPE)

    shapes = fields_by_note_type()

    assert {note_type: set(fields) for note_type, fields in shapes.items()} == WIRE_FIELDS_BY_TYPE


def test_field_values_survive_serialisation_with_their_json_types() -> None:
    shapes = fields_by_note_type()

    assert shapes["basic_optional_reversed"] == {"front": "Yield", "back": "Triangle", "addReverse": True}
    assert shapes["multiple_choice"] == {
        "question": "Which shape is a stop sign?",
        "answer": "Octagon",
        "distractors": ["Circle", "Square"],
    }
    assert shapes["image_occlusion"] == {
        "imageId": str(IMAGE_ID),
        "regions": [
            {"ordinal": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
            {"ordinal": 3, "x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5},
        ],
        "extra": "",
    }


def test_optional_media_and_source_fields_are_omitted_rather_than_null() -> None:
    [note] = [note for note in succeeded_notes() if note["noteType"] == "image_occlusion"]

    assert note["media"] == [
        {
            "mediaId": str(IMAGE_ID),
            "kind": "image",
            "url": "https://example.com/sign.png",
            "alt": "Warning sign",
            "width": 640,
            "height": 480,
            "license": "CC-BY-4.0",
        },
        {
            "mediaId": str(AUDIO.media_id),
            "kind": "audio",
            "url": "https://example.com/sign.mp3",
            "license": "CC0-1.0",
        },
    ]
    assert note["sources"] == [
        {"title": "Traffic code", "url": "https://example.com/code", "retrievedAt": "2026-08-14T10:30:20Z"},
        {"title": "Traffic regulations", "url": "https://example.com/rules"},
    ]
    assert note["tags"] == ["signs"]


def test_deck_without_a_description_omits_it_and_keeps_empty_collections() -> None:
    result = assert_polled(poll_stored(succeeded(BARE_RESULT)))["result"]

    assert result == {"deck": {"title": "Road signs", "tags": []}, "notes": []}


def test_deck_with_every_field_is_returned_whole() -> None:
    result = assert_polled(poll_stored(succeeded()))["result"]

    assert isinstance(result, dict)
    assert result["deck"] == {
        "title": "Road signs",
        "description": "Warning and prohibitory signs",
        "tags": ["driving", "signs"],
    }


def test_timestamps_with_another_offset_are_reported_in_utc() -> None:
    job = GenerationJob(
        job_id=JOB_ID,
        created_at=T0.astimezone(MOSCOW),
        updated_at=at(44).astimezone(MOSCOW),
        state=Running(stage=JobStage.PLANNING, progress=Progress(0.1)),
    )

    body = assert_polled(poll_stored(job))

    assert (body["createdAt"], body["updatedAt"]) == ("2026-08-14T10:30:00Z", "2026-08-14T10:30:44Z")


def test_job_created_through_the_api_can_be_polled() -> None:
    client = build_client(Harness())
    created = client.post(
        f"{API_PREFIX}/generations",
        json={"topic": "Road signs", "language": "ru", "cardCount": 40},
        headers={**HEADERS, "Idempotency-Key": "2c9e8f7a-6b5d-4c3e-8f1a-0b9c8d7e6f5a"},
    ).json()

    body = assert_polled(poll(client, created["jobId"]))

    assert body["jobId"] == created["jobId"]
    assert body["createdAt"] == created["createdAt"]
    assert (body["status"], body["stage"], body["progress"]) == ("queued", None, 0.0)
    assert body["updatedAt"] == body["createdAt"]


STEP = timedelta(seconds=1)
ADVANCES: tuple[tuple[JobStage, float], ...] = tuple(
    (stage, round(0.1 * position + offset, 2))
    for position, stage in enumerate(STAGE_ORDER, start=1)
    for offset in (0.0, 0.05)
)


def advance(job: GenerationJob, step: tuple[JobStage, float]) -> GenerationJob:
    stage, progress = step
    return job.advance(stage, progress, job.updated_at + STEP)


def advancing_jobs() -> list[GenerationJob]:
    advanced = list(accumulate(ADVANCES, advance, initial=queued().start(at(1))))
    return [queued(), *advanced, advanced[-1].succeed(FULL_RESULT, advanced[-1].updated_at + STEP)]


def test_progress_and_stage_never_move_backwards_across_a_sequence_of_polls() -> None:
    harness = Harness()
    client = build_client(harness)
    progress: list[float] = []
    stages: list[int] = []

    for job in advancing_jobs():
        harness.store.replace(job)
        body = assert_polled(poll(client, job.job_id))
        assert isinstance(body["progress"], float)
        progress.append(body["progress"])
        if isinstance(body["stage"], str):
            stages.append(JobStage(body["stage"]).position)

    assert progress == sorted(progress)
    assert stages == sorted(stages)
    assert len(set(stages)) == len(STAGE_ORDER)
    assert progress[-1] == 1.0


def test_unknown_job_is_job_not_found() -> None:
    assert_problem(poll(build_client(Harness()), UNKNOWN_JOB_ID), HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND")


MALFORMED_JOB_IDS = {
    "not a uuid": "not-a-uuid",
    "braced": f"{{{JOB_ID}}}",
    "urn": f"urn:uuid:{JOB_ID}",
    "unhyphenated": JOB_ID.hex,
    "trailing hyphen": f"{JOB_ID}-",
    "surrounding whitespace": f" {JOB_ID} ",
    "non-ascii digit": str(JOB_ID).replace("5", "\N{ARABIC-INDIC DIGIT FIVE}", 1),
    "overlong": f"{JOB_ID}0",
    "sql": "' OR 1=1 --",
}


@pytest.mark.parametrize("job_id", MALFORMED_JOB_IDS.values(), ids=MALFORMED_JOB_IDS.keys())
def test_malformed_job_id_is_job_not_found_even_when_its_uuid_exists(job_id: str) -> None:
    assert_problem(poll_stored_as(job_id), HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND")


def poll_stored_as(job_id: str) -> httpx2.Response:
    harness = Harness()
    harness.store.replace(queued())
    return poll(build_client(harness), job_id)


def test_uppercase_job_id_finds_the_job() -> None:
    body = assert_polled(poll_stored_as(str(JOB_ID).upper()))

    assert body["jobId"] == str(JOB_ID)


def test_non_version_4_job_id_is_job_not_found() -> None:
    response = poll(build_client(Harness()), "00000000-0000-0000-0000-000000000000")

    assert_problem(response, HTTPStatus.NOT_FOUND, "JOB_NOT_FOUND")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {CLIENT_ID_HEADER: ""},
        {CLIENT_ID_HEADER: "not-a-uuid"},
        {CLIENT_ID_HEADER: CLIENT_ID.upper()},
        {CLIENT_ID_HEADER: "00000000-0000-0000-0000-000000000000"},
    ],
    ids=["missing", "empty", "not a uuid", "uppercase", "nil"],
)
def test_missing_or_malformed_client_id_is_validation_failed(headers: dict[str, str]) -> None:
    harness = Harness()
    harness.store.replace(queued())

    response = poll(build_client(harness), JOB_ID, headers)

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")


def test_malformed_client_id_is_validation_failed_even_for_an_unknown_job() -> None:
    response = poll(build_client(Harness()), UNKNOWN_JOB_ID, {})

    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")


def test_polling_does_not_change_the_stored_job() -> None:
    harness = Harness()
    job = running()
    harness.store.replace(job)
    client = build_client(harness)

    for _ in range(3):
        assert_polled(poll(client, job.job_id))

    assert harness.store.jobs == {job.job_id: job}


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_wrong_method_on_the_job_route_is_method_not_allowed(method: str) -> None:
    response = build_client(Harness()).request(method, endpoint(JOB_ID), headers=HEADERS)

    assert_problem(response, HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")
    assert response.headers["allow"] == "GET"
