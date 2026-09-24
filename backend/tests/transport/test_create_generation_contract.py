import time
from http import HTTPStatus
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE
from deckly.main import API_PREFIX
from deckly.transport import generations
from deckly.transport.error_handlers import register_error_handlers
from deckly.transport.generations import CLIENT_ID_HEADER, IDEMPOTENCY_KEY_HEADER
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.fakes import Harness
from tests.transport.openapi import spec_errors

ENDPOINT = f"{API_PREFIX}/generations"
CLIENT_ID = "0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"
OTHER_CLIENT_ID = "8d1c2b3a-4f5e-4d6c-8b7a-9e0f1a2b3c4d"
IDEMPOTENCY_KEY = "2c9e8f7a-6b5d-4c3e-8f1a-0b9c8d7e6f5a"
VALID_HEADERS = {"Idempotency-Key": IDEMPOTENCY_KEY, "X-Client-Id": CLIENT_ID}
MINIMAL = {"topic": "Road signs", "language": "ru", "cardCount": 40}
REGISTERED_NOTE_TYPES = [str(note_type) for note_type in NOTE_FIELDS_BY_TYPE]


def build_client(harness: Harness) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(generations.router, prefix=API_PREFIX)
    app.state.create_generation = harness.create
    return TestClient(app, raise_server_exceptions=False)


def post(client: TestClient, payload: object, headers: dict[str, str] | None = None) -> httpx2.Response:
    return client.post(ENDPOINT, json=payload, headers=VALID_HEADERS if headers is None else headers)


def with_(**overrides: object) -> dict[str, object]:
    return {**MINIMAL, **overrides}


def without(field: str) -> dict[str, object]:
    return {key: value for key, value in MINIMAL.items() if key != field}


def assert_created(response: httpx2.Response) -> None:
    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    assert response.headers["content-type"] == "application/json"
    assert spec_errors("GenerationJobCreated", response.json()) == []


def assert_problem(response: httpx2.Response, status: HTTPStatus, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    body = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == code
    assert body["status"] == status


def assert_validation_failed(response: httpx2.Response) -> None:
    assert_problem(response, HTTPStatus.BAD_REQUEST, "VALIDATION_FAILED")


ACCEPTED_PAYLOADS: dict[str, dict[str, object]] = {
    "minimal": MINIMAL,
    "topic at 3": with_(topic="abc"),
    "topic at 200": with_(topic="x" * 200),
    "topic at 200 astral code points": with_(topic="\N{GRINNING FACE}" * 200),
    "topic at 3 after trimming": with_(topic="  abc  "),
    "topic with a zero-width joiner emoji": with_(
        topic="\N{WOMAN}\N{ZERO WIDTH JOINER}\N{PERSONAL COMPUTER} basics"
    ),
    "topic with combining marks on letters": with_(topic="Cafe\N{COMBINING ACUTE ACCENT} culture"),
    "topic in Devanagari with spacing marks": with_(topic="\u0939\u093f\u0928\u094d\u0926\u0940"),
    "cardCount at 5": with_(cardCount=5),
    "cardCount at 200": with_(cardCount=200),
    "every field": {
        "topic": "Road signs of the Russian traffic code",
        "language": "ru",
        "cardCount": 40,
        "difficulty": "advanced",
        "noteTypes": ["basic", "cloze", "multiple_choice"],
        "includeImages": True,
        "instructions": "Focus on warning and prohibitory signs",
    },
    "difficulty beginner": with_(difficulty="beginner"),
    "difficulty intermediate": with_(difficulty="intermediate"),
    "every registered note type": with_(noteTypes=REGISTERED_NOTE_TYPES),
    "includeImages false": with_(includeImages=False),
    "instructions empty": with_(instructions=""),
    "instructions at 500": with_(instructions="x" * 500),
    "language with region": with_(language="en-US"),
    "language with script and region": with_(language="zh-Hant-TW"),
    "language with numeric region": with_(language="es-419"),
    "language with variant": with_(language="de-CH-1996"),
    "language with extension and private use": with_(language="en-a-bbb-x-a-ccc"),
    "language private use only": with_(language="x-klingon"),
    "language grandfathered irregular": with_(language="i-klingon"),
    "language in any case": with_(language="EN-us"),
    "language with extlang": with_(language="zh-yue-HK"),
}

SPEC_AND_SERVER_REJECT: dict[str, object] = {
    "topic at 2": with_(topic="ab"),
    "topic at 201": with_(topic="x" * 201),
    "topic at 201 astral code points": with_(topic="\N{GRINNING FACE}" * 201),
    "topic missing": without("topic"),
    "topic not a string": with_(topic=12345),
    "topic null": with_(topic=None),
    "language missing": without("language"),
    "language not a string": with_(language=7),
    "cardCount at 4": with_(cardCount=4),
    "cardCount at 201": with_(cardCount=201),
    "cardCount zero": with_(cardCount=0),
    "cardCount negative": with_(cardCount=-5),
    "cardCount as string": with_(cardCount="40"),
    "cardCount fractional": with_(cardCount=40.5),
    "cardCount boolean": with_(cardCount=True),
    "cardCount missing": without("cardCount"),
    "cardCount null": with_(cardCount=None),
    "difficulty unknown": with_(difficulty="expert"),
    "difficulty wrong case": with_(difficulty="Advanced"),
    "difficulty null": with_(difficulty=None),
    "noteTypes empty": with_(noteTypes=[]),
    "noteTypes unknown": with_(noteTypes=["flashcard"]),
    "noteTypes duplicated": with_(noteTypes=["basic", "cloze", "basic"]),
    "noteTypes as string": with_(noteTypes="basic"),
    "noteTypes null": with_(noteTypes=None),
    "includeImages as string": with_(includeImages="true"),
    "includeImages as number": with_(includeImages=1),
    "includeImages null": with_(includeImages=None),
    "instructions at 501": with_(instructions="x" * 501),
    "instructions null": with_(instructions=None),
    "instructions not a string": with_(instructions=["focus"]),
    "unknown field": with_(model="gpt"),
    "snake_case field": {**without("cardCount"), "card_count": 40},
    "body is an array": [MINIMAL],
    "body is null": None,
    "body is a string": "Road signs",
}

SERVER_ONLY_REJECT: dict[str, dict[str, object]] = {
    "note type not registered yet": with_(noteTypes=["basic", "basic_optional_reversed"]),
    "language empty": with_(language=""),
    "language with underscore": with_(language="en_US"),
    "language single letter": with_(language="e"),
    "language too long a subtag": with_(language="abcdefghi"),
    "language trailing hyphen": with_(language="en-"),
    "language leading hyphen": with_(language="-en"),
    "language empty subtag": with_(language="en--US"),
    "language with space": with_(language="en US"),
    "language with trailing newline": with_(language="en\n"),
    "language digits": with_(language="123"),
    "language non-ascii letter folding to ascii": with_(language="en-u\N{LATIN SMALL LETTER LONG S}"),
    "language kelvin sign": with_(language="en-\N{KELVIN SIGN}R"),
    "language cyrillic": with_(language="\N{CYRILLIC SMALL LETTER ER}\N{CYRILLIC SMALL LETTER U}"),
    "language natural name": with_(language="Russian language"),
    "language irregular tag with kelvin sign": with_(language="i-\N{KELVIN SIGN}lingon"),
    "topic whitespace only": with_(topic="   "),
    "topic of mixed whitespace": with_(topic=" \t\n\r\u3000\u00a0 "),
    "topic at 2 after trimming": with_(topic="  ab  "),
    "topic of zero-width spaces only": with_(topic="\N{ZERO WIDTH SPACE}" * 5),
    "topic of byte order marks only": with_(topic="\N{ZERO WIDTH NO-BREAK SPACE}" * 5),
    "topic of invisible format characters": with_(
        topic="\N{ZERO WIDTH JOINER}\N{ZERO WIDTH NON-JOINER}\N{LEFT-TO-RIGHT MARK}\N{WORD JOINER}"
    ),
    "topic of control characters": with_(topic="\x01\x7f\x1b\u0085"),
    "topic of combining marks only": with_(
        topic="\N{COMBINING ACUTE ACCENT}\N{COMBINING GRAVE ACCENT}\N{COMBINING ENCLOSING CIRCLE}"
    ),
    "topic of spacing combining marks only": with_(topic="\u093e\u093f\u0940"),
    "topic of invisibles padded with whitespace": with_(
        topic="  \N{ZERO WIDTH SPACE}\N{ZERO WIDTH NO-BREAK SPACE}\t\N{ZERO WIDTH SPACE}  "
    ),
    "topic with NUL": with_(topic="Road\x00signs"),
    "instructions with NUL": with_(instructions="Focus\x00"),
}

ADVERSARIAL_LANGUAGE_TAGS = {
    "many variants then garbage": "de" + "-12345" * 20_000 + "!",
    "many extensions then garbage": "en" + "-a-bb" * 20_000 + "!",
    "many private use subtags then garbage": "x" + "-a" * 40_000 + "!",
    "many extlang-sized subtags": "zh" + "-abc" * 40_000,
}
MAX_VALIDATION_SECONDS = 1.0


UNPARSEABLE_BODIES: dict[str, str | bytes] = {
    "cardCount beyond the integer digit limit": '{"topic": "Road signs", "language": "ru", "cardCount": '
    + "9" * 5000
    + "}",
    "truncated json": '{"topic": "Road signs", "language": "ru", "cardCount": ',
    "invalid utf-8": b'{"topic": "Road \xff signs", "language": "ru", "cardCount": 40}',
    "nesting beyond the recursion limit": '{"topic": ' + "[" * 100_000 + "]" * 100_000 + "}",
}


@pytest.mark.parametrize("payload", ACCEPTED_PAYLOADS.values(), ids=ACCEPTED_PAYLOADS.keys())
def test_payload_the_spec_accepts_is_accepted(payload: dict[str, object]) -> None:
    assert spec_errors("GenerationRequest", payload) == []

    assert_created(post(build_client(Harness()), payload))


@pytest.mark.parametrize("payload", SPEC_AND_SERVER_REJECT.values(), ids=SPEC_AND_SERVER_REJECT.keys())
def test_payload_the_spec_rejects_is_validation_failed(payload: object) -> None:
    assert spec_errors("GenerationRequest", payload) != []

    assert_validation_failed(post(build_client(Harness()), payload))


@pytest.mark.parametrize("payload", SERVER_ONLY_REJECT.values(), ids=SERVER_ONLY_REJECT.keys())
def test_rules_beyond_the_schema_are_validation_failed(payload: dict[str, object]) -> None:
    assert_validation_failed(post(build_client(Harness()), payload))


@pytest.mark.parametrize("language", ADVERSARIAL_LANGUAGE_TAGS.values(), ids=ADVERSARIAL_LANGUAGE_TAGS.keys())
def test_pathological_language_tag_is_rejected_quickly(language: str) -> None:
    client = build_client(Harness())

    started = time.perf_counter()
    response = post(client, with_(language=language))

    assert time.perf_counter() - started < MAX_VALIDATION_SECONDS
    assert_validation_failed(response)


@pytest.mark.parametrize("content", UNPARSEABLE_BODIES.values(), ids=UNPARSEABLE_BODIES.keys())
def test_unparseable_body_is_validation_failed_not_internal_error(content: str | bytes) -> None:
    harness = Harness()

    response = build_client(harness).post(
        ENDPOINT, content=content, headers={**VALID_HEADERS, "content-type": "application/json"}
    )

    assert_validation_failed(response)
    assert harness.store.jobs == {}


def test_rejected_request_creates_and_enqueues_nothing() -> None:
    harness = Harness()

    post(build_client(harness), with_(noteTypes=["basic_optional_reversed"]))

    assert harness.store.jobs == {}
    assert harness.queue.enqueued == []


def test_topic_is_stored_trimmed() -> None:
    harness = Harness()

    body = post(build_client(harness), with_(topic="  Road signs \n")).json()

    assert harness.store.requests[UUID(body["jobId"])].topic == "Road signs"


def test_defaults_are_applied_to_the_stored_request() -> None:
    harness = Harness()

    body = post(build_client(harness), MINIMAL).json()

    stored = harness.store.requests[UUID(body["jobId"])]
    assert stored.difficulty == "intermediate"
    assert stored.note_types == ("basic",)
    assert stored.include_images is False
    assert stored.instructions is None


def test_created_job_is_queued_with_an_offset_timestamp_and_a_quota() -> None:
    response = post(build_client(Harness()), MINIMAL)

    assert_created(response)
    body = response.json()
    assert body["status"] == "queued"
    assert body["createdAt"].endswith("Z")
    assert set(body["quota"]) == {"limit", "remaining", "resetsAt"}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Client-Id": CLIENT_ID},
        {"Idempotency-Key": IDEMPOTENCY_KEY},
        {},
        {"Idempotency-Key": "not-a-uuid", "X-Client-Id": CLIENT_ID},
        {"Idempotency-Key": IDEMPOTENCY_KEY, "X-Client-Id": "not-a-uuid"},
        {"Idempotency-Key": "", "X-Client-Id": CLIENT_ID},
        {"Idempotency-Key": IDEMPOTENCY_KEY, "X-Client-Id": ""},
    ],
    ids=[
        "missing idempotency key",
        "missing client id",
        "missing both",
        "idempotency key not a uuid",
        "client id not a uuid",
        "idempotency key empty",
        "client id empty",
    ],
)
def test_missing_or_malformed_headers_are_validation_failed(headers: dict[str, str]) -> None:
    harness = Harness()

    assert_validation_failed(post(build_client(harness), MINIMAL, headers))
    assert harness.store.jobs == {}


NON_CANONICAL_UUIDS = {
    "uppercase": IDEMPOTENCY_KEY.upper(),
    "braced": f"{{{IDEMPOTENCY_KEY}}}",
    "urn": f"urn:uuid:{IDEMPOTENCY_KEY}",
    "unhyphenated": IDEMPOTENCY_KEY.replace("-", ""),
    "hyphens misplaced": IDEMPOTENCY_KEY.replace("-", "")[:4] + "-" + IDEMPOTENCY_KEY.replace("-", "")[4:],
    "extra hyphen": IDEMPOTENCY_KEY + "-",
}


@pytest.mark.parametrize("header", [IDEMPOTENCY_KEY_HEADER, CLIENT_ID_HEADER])
@pytest.mark.parametrize("value", NON_CANONICAL_UUIDS.values(), ids=NON_CANONICAL_UUIDS.keys())
def test_non_canonical_uuid_header_is_validation_failed(header: str, value: str) -> None:
    harness = Harness()

    response = post(build_client(harness), MINIMAL, {**VALID_HEADERS, header: value})

    assert_validation_failed(response)
    assert harness.store.jobs == {}


NON_V4_UUIDS = {
    "nil": "00000000-0000-0000-0000-000000000000",
    "max": "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "version 1": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "version 7": "01890a5d-ac96-774b-bcce-b302099a8057",
    "version 4 with the NCS variant": "2c9e8f7a-6b5d-4c3e-0f1a-0b9c8d7e6f5a",
    "version 4 with the Microsoft variant": "2c9e8f7a-6b5d-4c3e-cf1a-0b9c8d7e6f5a",
}


@pytest.mark.parametrize("header", [IDEMPOTENCY_KEY_HEADER, CLIENT_ID_HEADER])
@pytest.mark.parametrize("value", NON_V4_UUIDS.values(), ids=NON_V4_UUIDS.keys())
def test_canonical_uuid_header_that_is_not_version_4_is_validation_failed(header: str, value: str) -> None:
    harness = Harness()

    response = post(build_client(harness), MINIMAL, {**VALID_HEADERS, header: value})

    assert_validation_failed(response)
    assert harness.store.jobs == {}


def test_replaying_the_same_key_for_the_same_client_returns_the_same_job() -> None:
    harness = Harness()
    client = build_client(harness)

    first = post(client, MINIMAL)
    replay = post(client, MINIMAL)

    assert_created(replay)
    assert replay.json()["jobId"] == first.json()["jobId"]
    assert replay.json()["createdAt"] == first.json()["createdAt"]
    assert len(harness.store.jobs) == 1


EQUIVALENT_REPLAYS: dict[str, dict[str, object]] = {
    "fields reordered": {"cardCount": 40, "language": "ru", "topic": "Road signs"},
    "defaults spelled out": with_(difficulty="intermediate", noteTypes=["basic"], includeImages=False),
    "topic padded with whitespace": with_(topic="  Road signs\n"),
}


@pytest.mark.parametrize("replay", EQUIVALENT_REPLAYS.values(), ids=EQUIVALENT_REPLAYS.keys())
def test_replaying_an_equivalent_body_returns_the_same_job(replay: dict[str, object]) -> None:
    harness = Harness()
    client = build_client(harness)

    first = post(client, MINIMAL)
    second = post(client, replay)

    assert_created(second)
    assert second.json()["jobId"] == first.json()["jobId"]


DIFFERENT_REPLAYS: dict[str, tuple[dict[str, object], dict[str, object]]] = {
    "topic": (MINIMAL, with_(topic="A different topic")),
    "topic case": (MINIMAL, with_(topic="road signs")),
    "language": (MINIMAL, with_(language="en")),
    "language case": (MINIMAL, with_(language="RU")),
    "cardCount": (MINIMAL, with_(cardCount=41)),
    "difficulty": (MINIMAL, with_(difficulty="advanced")),
    "noteTypes": (MINIMAL, with_(noteTypes=["cloze"])),
    "noteTypes order": (with_(noteTypes=["basic", "cloze"]), with_(noteTypes=["cloze", "basic"])),
    "includeImages": (MINIMAL, with_(includeImages=True)),
    "instructions added": (MINIMAL, with_(instructions="Focus on warning signs")),
    "instructions empty": (MINIMAL, with_(instructions="")),
}


@pytest.mark.parametrize(("original", "replay"), DIFFERENT_REPLAYS.values(), ids=DIFFERENT_REPLAYS.keys())
def test_replaying_the_key_with_a_different_body_is_a_conflict(
    original: dict[str, object], replay: dict[str, object]
) -> None:
    harness = Harness()
    client = build_client(harness)
    first = post(client, original)

    response = post(client, replay)

    assert_problem(response, HTTPStatus.CONFLICT, "IDEMPOTENCY_KEY_CONFLICT")
    assert list(harness.store.jobs) == [UUID(first.json()["jobId"])]
    assert harness.queue.enqueued == [UUID(first.json()["jobId"])]


def test_original_body_still_replays_after_a_conflict() -> None:
    harness = Harness()
    client = build_client(harness)
    first = post(client, MINIMAL)

    post(client, with_(topic="A different topic"))
    replay = post(client, MINIMAL)

    assert_created(replay)
    assert replay.json()["jobId"] == first.json()["jobId"]


def test_unknown_route_is_route_not_found() -> None:
    response = build_client(Harness()).post(f"{API_PREFIX}/generation", json=MINIMAL, headers=VALID_HEADERS)

    assert_problem(response, HTTPStatus.NOT_FOUND, "ROUTE_NOT_FOUND")


def test_wrong_method_on_an_existing_route_is_method_not_allowed() -> None:
    harness = Harness()

    response = build_client(harness).put(ENDPOINT, json=MINIMAL, headers=VALID_HEADERS)

    assert_problem(response, HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED")
    assert response.headers["allow"] == "POST"
    assert harness.store.jobs == {}


def test_same_key_from_a_different_client_is_a_different_job() -> None:
    harness = Harness()
    client = build_client(harness)

    first = post(client, MINIMAL)
    other = post(client, MINIMAL, {"Idempotency-Key": IDEMPOTENCY_KEY, "X-Client-Id": OTHER_CLIENT_ID})

    assert_created(other)
    assert other.json()["jobId"] != first.json()["jobId"]
    assert len(harness.store.jobs) == 2
