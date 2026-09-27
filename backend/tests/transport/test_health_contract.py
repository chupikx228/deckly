import re
from http import HTTPStatus

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.health import CheckHealth
from deckly.main import API_PREFIX
from deckly.transport import health
from deckly.transport.error_handlers import register_error_handlers
from deckly.transport.problem import PROBLEM_JSON_MEDIA_TYPE
from tests.domain.builders import T0
from tests.fakes import QUOTA_LIMIT, FixedQuota
from tests.transport.openapi import declared_responses, spec_contents, spec_errors

ENDPOINT = f"{API_PREFIX}/health"
SPEC_PATH = "/health"
SPEC_METHOD = "get"
VERSION = "1.2.3"
CLIENT_ID = "0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"


class StubProbe:
    def __init__(self, *, healthy: bool) -> None:
        self.healthy = healthy
        self.calls = 0

    async def is_healthy(self) -> bool:
        self.calls += 1
        return self.healthy


def build_client(*probes: StubProbe) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(health.router, prefix=API_PREFIX)
    app.state.check_health = CheckHealth(probes=probes, quota=FixedQuota(), clock=lambda: T0, version=VERSION)
    return TestClient(app, raise_server_exceptions=False)


def get(client: TestClient, headers: dict[str, str] | None = None) -> httpx2.Response:
    return client.get(ENDPOINT, headers=headers or {})


def assert_health(response: httpx2.Response) -> dict[str, object]:
    assert response.status_code == HTTPStatus.OK, response.text
    assert response.headers["content-type"] == "application/json"
    assert str(response.status_code) in declared_responses(SPEC_PATH, SPEC_METHOD)
    body: dict[str, object] = response.json()
    assert spec_errors("Health", body) == []
    return body


def assert_validation_failed(response: httpx2.Response) -> None:
    assert response.status_code == HTTPStatus.BAD_REQUEST, response.text
    assert response.headers["content-type"] == PROBLEM_JSON_MEDIA_TYPE
    assert str(response.status_code) in declared_responses(SPEC_PATH, SPEC_METHOD)
    body: dict[str, object] = response.json()
    assert spec_errors("Problem", body) == []
    assert body["code"] == "VALIDATION_FAILED"


def test_health_without_a_client_id_has_no_quota() -> None:
    body = assert_health(get(build_client(StubProbe(healthy=True), StubProbe(healthy=True))))

    assert body == {"status": "ok", "version": VERSION}


def test_health_with_a_client_id_carries_its_quota() -> None:
    body = assert_health(get(build_client(StubProbe(healthy=True)), {"X-Client-Id": CLIENT_ID}))

    assert body == {
        "status": "ok",
        "version": VERSION,
        "quota": {"limit": QUOTA_LIMIT, "remaining": QUOTA_LIMIT, "resetsAt": "2026-08-15T10:30:00Z"},
    }


@pytest.mark.parametrize("unhealthy", [0, 1])
def test_any_unhealthy_dependency_makes_the_service_degraded(unhealthy: int) -> None:
    probes = [StubProbe(healthy=True), StubProbe(healthy=True)]
    probes[unhealthy].healthy = False

    body = assert_health(get(build_client(*probes), {"X-Client-Id": CLIENT_ID}))

    assert body["status"] == "degraded"
    assert "quota" in body
    assert [probe.calls for probe in probes] == [1, 1]


def test_client_id_header_name_is_case_insensitive() -> None:
    body = assert_health(get(build_client(StubProbe(healthy=True)), {"x-client-id": CLIENT_ID}))

    assert "quota" in body


MALFORMED_CLIENT_IDS = {
    "empty": "",
    "not a uuid": "not-a-uuid",
    "uppercase": CLIENT_ID.upper(),
    "braced": f"{{{CLIENT_ID}}}",
    "urn": f"urn:uuid:{CLIENT_ID}",
    "unhyphenated": CLIENT_ID.replace("-", ""),
    "nil": "00000000-0000-0000-0000-000000000000",
    "version 1": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "version 7": "01890a5d-ac96-774b-bcce-b302099a8057",
    "version 4 with the NCS variant": "2c9e8f7a-6b5d-4c3e-0f1a-0b9c8d7e6f5a",
}


@pytest.mark.parametrize("value", MALFORMED_CLIENT_IDS.values(), ids=MALFORMED_CLIENT_IDS.keys())
def test_malformed_client_id_is_validation_failed_and_skips_the_checks(value: str) -> None:
    probe = StubProbe(healthy=True)

    assert_validation_failed(get(build_client(probe), {"X-Client-Id": value}))
    assert probe.calls == 0


def spec_client_id_pattern() -> str:
    node = spec_contents()
    for key in ("paths", SPEC_PATH, SPEC_METHOD, "parameters"):
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, list)
    [parameter] = node
    assert parameter["name"] == "X-Client-Id"
    assert parameter["required"] is False
    pattern = parameter["schema"]["pattern"]
    assert isinstance(pattern, str)
    return pattern


def test_spec_pattern_accepts_the_client_id_the_server_accepts() -> None:
    assert re.search(spec_client_id_pattern(), CLIENT_ID) is not None


@pytest.mark.parametrize("value", MALFORMED_CLIENT_IDS.values(), ids=MALFORMED_CLIENT_IDS.keys())
def test_spec_pattern_rejects_what_the_server_rejects(value: str) -> None:
    assert re.search(spec_client_id_pattern(), value) is None
