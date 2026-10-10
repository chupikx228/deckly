from http import HTTPStatus

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.exceptions import NoValidContentError, RateLimitedError, UpstreamUnavailableError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.regeneration import RejectionReason
from deckly.infrastructure.card_generator.regeneration_prompt import rejected_card
from deckly.main import API_PREFIX
from deckly.transport import notes
from deckly.transport.error_handlers import RETRY_AFTER_HEADER, register_error_handlers
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.domain.builders import FIELDS_BY_TYPE, client_id
from tests.fakes import RegenerationHarness, RegenerationStep
from tests.transport.openapi import declared_responses, spec_errors

ENDPOINT = f"{API_PREFIX}/notes/regenerate"
SPEC_PATH = "/notes/regenerate"
SPEC_METHOD = "post"
CLIENT_ID = "0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"
VALID_HEADERS = {"X-Client-Id": CLIENT_ID}
MINIMAL: dict[str, object] = {
    "topic": "Road signs of the Russian traffic code",
    "language": "ru",
    "noteType": "basic",
    "rejectedNote": {"fields": {"front": "What does a red triangle warn of?", "back": "A hazard"}},
    "reason": "too_easy",
}
REGENERATABLE_TYPES = [str(note_type) for note_type in NoteType if note_type is not NoteType.IMAGE_OCCLUSION]


def build_client(harness: RegenerationHarness) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(notes.router, prefix=API_PREFIX)
    app.state.regenerate_note = harness.regenerate
    return TestClient(app, raise_server_exceptions=False)


def post(client: TestClient, payload: object, headers: dict[str, str] | None = None) -> httpx2.Response:
    return client.post(ENDPOINT, json=payload, headers=VALID_HEADERS if headers is None else headers)


def with_(**overrides: object) -> dict[str, object]:
    return {**MINIMAL, **overrides}


def without(field: str) -> dict[str, object]:
    return {key: value for key, value in MINIMAL.items() if key != field}


def rejected(fields: object, **extra: object) -> dict[str, object]:
    return with_(rejectedNote={"fields": fields, **extra})


def assert_declared(response: httpx2.Response) -> None:
    assert str(response.status_code) in declared_responses(SPEC_PATH, SPEC_METHOD), response.text


def assert_regenerated(response: httpx2.Response) -> dict[str, object]:
    assert response.status_code == HTTPStatus.OK, response.text
    assert response.headers["content-type"] == "application/json"
    assert_declared(response)
    body: dict[str, object] = response.json()
    assert spec_errors("GeneratedNote", body) == []
    return body


def assert_problem(response: httpx2.Response, status: HTTPStatus, code: str) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body: dict[str, object] = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == code
    assert body["status"] == status
    return body


def assert_validation_failed(response: httpx2.Response) -> None:
    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")
    assert_declared(response)


def test_reason_enum_matches_the_spec() -> None:
    for reason in RejectionReason:
        assert spec_errors("RejectionReason", str(reason)) == []
    assert spec_errors("RejectionReason", "boring") != []


@pytest.mark.parametrize("reason", [str(reason) for reason in RejectionReason])
def test_every_reason_regenerates_a_note_matching_the_spec(reason: str) -> None:
    payload = with_(reason=reason)
    assert spec_errors("RegenerateNoteRequest", payload) == []
    harness = RegenerationHarness()

    body = assert_regenerated(post(build_client(harness), payload))

    assert body["clientId"] == str(harness.providers.note.client_id)
    assert body["sources"] == [{"title": "Traffic regulations", "url": "https://example.com/rules"}]
    [(_, request, _)] = harness.providers.regenerated
    assert str(request.reason) == reason


@pytest.mark.parametrize("note_type", REGENERATABLE_TYPES)
def test_every_note_type_built_without_an_image_is_regenerated(note_type: str) -> None:
    harness = RegenerationHarness()
    harness.providers.note = harness.providers.note.__class__(
        client_id=client_id(7),
        fields=FIELDS_BY_TYPE[NoteType(note_type)],
        sources=harness.providers.note.sources,
    )
    payload = with_(noteType=note_type)
    assert spec_errors("RegenerateNoteRequest", payload) == []

    body = assert_regenerated(post(build_client(harness), payload))

    assert body["noteType"] == note_type
    [(_, request, _)] = harness.providers.regenerated
    assert request.note_type == note_type


def test_request_reaches_the_use_case_intact() -> None:
    harness = RegenerationHarness()

    assert_regenerated(post(build_client(harness), with_(topic="  Road signs  ", language="en-GB")))

    [(_, request, _)] = harness.providers.regenerated
    assert (request.topic, request.language) == ("Road signs", "en-GB")
    assert request.rejected_fields == {"front": "What does a red triangle warn of?", "back": "A hazard"}
    assert [str(client) for client, _ in harness.providers.acquired] == [CLIENT_ID]


ACCEPTED_PAYLOADS: dict[str, dict[str, object]] = {
    "minimal": MINIMAL,
    "topic at 3": with_(topic="abc"),
    "topic at 200": with_(topic="x" * 200),
    "rejected note with the rest of the generated note": rejected(
        {"front": "a", "back": "b"},
        clientId="8d1c2b3a-4f5e-4d6c-8b7a-9e0f1a2b3c4d",
        sources=[{"title": "Rules", "url": "https://example.com"}],
        tags=["x"],
    ),
    "rejected fields empty": rejected({}),
    "rejected fields of another shape": rejected({"question": "Q", "distractors": ["a", "b"], "n": 1}),
    "rejected fields with nested values": rejected({"front": {"a": [1, None, True, 2.5]}}),
    "language with region": with_(language="en-US"),
}

SPEC_AND_SERVER_REJECT: dict[str, object] = {
    "reason missing": without("reason"),
    "reason null": with_(reason=None),
    "reason unknown": with_(reason="boring"),
    "reason wrong case": with_(reason="TOO_EASY"),
    "reason as a number": with_(reason=1),
    "noteType missing": without("noteType"),
    "noteType unknown": with_(noteType="flashcard"),
    "noteType null": with_(noteType=None),
    "rejectedNote missing": without("rejectedNote"),
    "rejectedNote null": with_(rejectedNote=None),
    "rejectedNote not an object": with_(rejectedNote=["front", "back"]),
    "rejectedNote without fields": with_(rejectedNote={}),
    "fields null": rejected(None),
    "fields a list": rejected(["front", "back"]),
    "fields a string": rejected("front: back"),
    "topic missing": without("topic"),
    "topic at 2": with_(topic="ab"),
    "topic at 201": with_(topic="x" * 201),
    "topic not a string": with_(topic=12345),
    "language missing": without("language"),
    "language not a string": with_(language=7),
    "unknown field": with_(cardCount=1),
    "body is an array": [MINIMAL],
    "body is null": None,
}

SERVER_ONLY_REJECT: dict[str, dict[str, object]] = {
    "image_occlusion": with_(noteType="image_occlusion"),
    "language with underscore": with_(language="en_US"),
    "language empty": with_(language=""),
    "topic whitespace only": with_(topic="   "),
    "topic of zero-width spaces only": with_(topic="\N{ZERO WIDTH SPACE}" * 5),
    "topic with NUL": with_(topic="Road\x00signs"),
}


@pytest.mark.parametrize("payload", ACCEPTED_PAYLOADS.values(), ids=ACCEPTED_PAYLOADS.keys())
def test_payload_the_spec_accepts_is_regenerated(payload: dict[str, object]) -> None:
    assert spec_errors("RegenerateNoteRequest", payload) == []

    assert_regenerated(post(build_client(RegenerationHarness()), payload))


@pytest.mark.parametrize("payload", SPEC_AND_SERVER_REJECT.values(), ids=SPEC_AND_SERVER_REJECT.keys())
def test_payload_the_spec_rejects_is_validation_failed(payload: object) -> None:
    assert spec_errors("RegenerateNoteRequest", payload) != []
    harness = RegenerationHarness()

    assert_validation_failed(post(build_client(harness), payload))
    assert harness.providers.steps == []


@pytest.mark.parametrize("payload", SERVER_ONLY_REJECT.values(), ids=SERVER_ONLY_REJECT.keys())
def test_rules_beyond_the_schema_are_validation_failed_before_any_provider_is_called(
    payload: dict[str, object],
) -> None:
    harness = RegenerationHarness()

    assert_validation_failed(post(build_client(harness), payload))
    assert harness.providers.steps == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Client-Id": "not-a-uuid"},
        {"X-Client-Id": CLIENT_ID.upper()},
        {"X-Client-Id": "0b6f7c1e-4a3d-1f2e-9c8b-7a6d5e4f3a21"},
    ],
    ids=["missing", "not a uuid", "uppercase", "version 1"],
)
def test_request_without_a_canonical_client_id_is_validation_failed(headers: dict[str, str]) -> None:
    harness = RegenerationHarness()

    assert_validation_failed(post(build_client(harness), MINIMAL, headers))
    assert harness.providers.steps == []


def test_rate_limited_client_gets_a_429_with_when_to_retry() -> None:
    harness = RegenerationHarness()
    harness.providers.failures[RegenerationStep.LIMIT] = RateLimitedError(600)

    response = post(build_client(harness), MINIMAL)

    body = assert_problem(response, HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED")
    assert_declared(response)
    assert body["retryAfterSeconds"] == 600
    assert response.headers[RETRY_AFTER_HEADER] == "600"


@pytest.mark.parametrize(
    "step",
    [
        RegenerationStep.LIMIT,
        RegenerationStep.SEARCH,
        RegenerationStep.SCREEN_REQUEST,
        RegenerationStep.GENERATE,
        RegenerationStep.SCREEN_NOTE,
    ],
)
def test_provider_outage_is_a_503_with_when_to_retry(step: RegenerationStep) -> None:
    harness = RegenerationHarness()
    harness.providers.failures[step] = UpstreamUnavailableError(4)

    response = post(build_client(harness), MINIMAL)

    body = assert_problem(response, HTTPStatus.SERVICE_UNAVAILABLE, "UPSTREAM_UNAVAILABLE")
    assert_declared(response)
    assert body["retryAfterSeconds"] == 4
    assert response.headers[RETRY_AFTER_HEADER] == "4"


def test_model_output_with_no_usable_note_is_a_503_no_valid_content() -> None:
    harness = RegenerationHarness()
    harness.providers.failures[RegenerationStep.GENERATE] = NoValidContentError("all dropped")

    response = post(build_client(harness), MINIMAL)

    body = assert_problem(response, HTTPStatus.SERVICE_UNAVAILABLE, "NO_VALID_CONTENT")
    assert_declared(response)
    assert "retryAfterSeconds" not in body


def test_rejected_topic_or_rejected_card_is_a_422_topic_rejected() -> None:
    harness = RegenerationHarness()
    harness.providers.request_allowed = False

    response = post(build_client(harness), MINIMAL)

    body = assert_problem(response, HTTPStatus.UNPROCESSABLE_ENTITY, "TOPIC_REJECTED")
    assert_declared(response)
    assert "retryAfterSeconds" not in body
    assert harness.providers.regenerated == []


def test_regenerated_note_the_content_check_blocks_is_a_503_no_valid_content() -> None:
    harness = RegenerationHarness()
    harness.providers.note_allowed = False

    response = post(build_client(harness), MINIMAL)

    body = assert_problem(response, HTTPStatus.SERVICE_UNAVAILABLE, "NO_VALID_CONTENT")
    assert_declared(response)
    assert "retryAfterSeconds" not in body
    assert "fields" not in body


def test_unexpected_failure_is_a_clean_internal_error_problem() -> None:
    harness = RegenerationHarness()
    harness.providers.failures[RegenerationStep.PARSE] = RuntimeError("bug")

    assert_problem(post(build_client(harness), MINIMAL), HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR")


def test_other_methods_are_not_allowed() -> None:
    response = build_client(RegenerationHarness()).get(ENDPOINT, headers=VALID_HEADERS)

    assert_problem(response, HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")


def test_spec_declares_every_status_this_endpoint_can_answer_with_a_problem() -> None:
    assert {"200", "400", "422", "429", "503"} <= set(declared_responses(SPEC_PATH, SPEC_METHOD))


def test_rejected_fields_with_nul_and_lone_surrogates_are_accepted_as_data() -> None:
    harness = RegenerationHarness()
    content = (
        '{"topic": "Road signs", "language": "ru", "noteType": "basic", "reason": "incorrect", '
        '"rejectedNote": {"fields": {"front": "a\\u0000b", "back": "\\ud800x\\udfff"}}}'
    )

    response = build_client(harness).post(
        ENDPOINT, content=content, headers={**VALID_HEADERS, "content-type": "application/json"}
    )

    assert_regenerated(response)
    [(_, request, _)] = harness.providers.regenerated
    assert request.rejected_fields == {"front": "a\x00b", "back": "\ud800x\udfff"}


def test_topic_of_lone_surrogates_that_would_leave_no_deck_title_is_validation_failed() -> None:
    harness = RegenerationHarness()
    content = (
        '{"topic": "\\ud800\\ud800\\ud800", "language": "ru", "noteType": "basic", "reason": "incorrect", '
        '"rejectedNote": {"fields": {"front": "a", "back": "b"}}}'
    )

    response = build_client(harness).post(
        ENDPOINT, content=content, headers={**VALID_HEADERS, "content-type": "application/json"}
    )

    assert_validation_failed(response)
    assert harness.providers.steps == []


UNPARSEABLE_BODIES: dict[str, str | bytes] = {
    "truncated json": '{"topic": "Road signs", "language": "ru", ',
    "invalid utf-8": b'{"topic": "Road \xff signs", "language": "ru"}',
    "fields nested beyond the recursion limit": (
        '{"topic": "Road signs", "language": "ru", "noteType": "basic", "reason": "other", '
        '"rejectedNote": {"fields": {"front": ' + "[" * 100_000 + "]" * 100_000 + "}}}"
    ),
}


@pytest.mark.parametrize("content", UNPARSEABLE_BODIES.values(), ids=UNPARSEABLE_BODIES.keys())
def test_unparseable_body_is_validation_failed_not_internal_error(content: str | bytes) -> None:
    harness = RegenerationHarness()

    response = build_client(harness).post(
        ENDPOINT, content=content, headers={**VALID_HEADERS, "content-type": "application/json"}
    )

    assert_validation_failed(response)
    assert harness.providers.steps == []


@pytest.mark.parametrize("depth", [50, 250, 300, 900])
def test_deeply_nested_rejected_fields_are_refused_or_safe_to_put_in_a_prompt(depth: int) -> None:
    harness = RegenerationHarness()
    content = (
        '{"topic": "Road signs", "language": "ru", "noteType": "basic", "reason": "other", '
        '"rejectedNote": {"fields": {"front": ' + "[" * depth + "]" * depth + "}}}"
    )

    response = build_client(harness).post(
        ENDPOINT, content=content, headers={**VALID_HEADERS, "content-type": "application/json"}
    )

    if response.status_code == HTTPStatus.OK:
        [(_, request, _)] = harness.providers.regenerated
        assert rejected_card(request.rejected_fields).startswith("<rejected_card>")
    else:
        assert_validation_failed(response)
