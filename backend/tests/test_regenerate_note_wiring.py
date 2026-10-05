import asyncio
import os
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.regeneration import RegenerateNote
from deckly.config import Settings
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.llm.client import LlmRejectedError, LlmResponseError, LlmUnavailableError
from deckly.infrastructure.provider_faults import PROVIDER_FAULT_RETRY_AFTER_SECONDS
from deckly.infrastructure.search.client import (
    SearchHit,
    SearchRejectedError,
    SearchResponseError,
    SearchUnavailableError,
)
from deckly.main import API_PREFIX, RegenerationClients, build_regenerate_note, single_attempt
from deckly.transport import notes
from deckly.transport.error_handlers import register_error_handlers
from tests.fakes import (
    FakeLlmClient,
    FakeSearchClient,
    fresh_observability,
    hang_forever,
    model_reply,
    regeneration_request,
)
from tests.test_config import settings_from_environment
from tests.transport.openapi import spec_errors

ENDPOINT = f"{API_PREFIX}/notes/regenerate"
HEADERS = {"X-Client-Id": "0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"}
PAYLOAD = {
    "topic": "Road signs",
    "language": "en",
    "noteType": "basic",
    "rejectedNote": {"fields": {"front": "What does a red triangle warn of?", "back": "A hazard"}},
    "reason": "too_easy",
}
HIT = SearchHit(
    title="Road signs",
    url="https://example.com/signs",
    content="A red circle on a road sign prohibits something. A stop sign is a red octagon.",
)
REPLY = model_reply(
    {
        "notes": [
            {
                "noteType": "basic",
                "fields": {"front": "Shape of a stop sign?", "back": "Octagon"},
                "sources": [1],
            }
        ]
    }
)
CONCURRENT_CALLS = 5


class OpenLimiter:
    def __init__(self) -> None:
        self.acquired: list[UUID] = []

    async def acquire(self, client_id: UUID, now: datetime) -> None:
        del now
        self.acquired.append(client_id)


async def never_answers() -> tuple[SearchHit, ...]:
    await asyncio.Event().wait()
    message = "a hanging search never returns"
    raise AssertionError(message)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    for name in [name for name in os.environ if name.startswith("DECKLY_")]:
        monkeypatch.delenv(name)
    return settings_from_environment(monkeypatch)


def regenerate_note(settings: Settings, llm: FakeLlmClient, search: FakeSearchClient) -> RegenerateNote:
    return build_regenerate_note(
        settings,
        RegenerationClients(
            llm=llm, search=search, limiter=OpenLimiter(), observability=fresh_observability()
        ),
    )


def client_for(regenerate: RegenerateNote) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(notes.router, prefix=API_PREFIX)
    app.state.regenerate_note = regenerate
    return TestClient(app, raise_server_exceptions=False)


def timed_post(client: TestClient) -> tuple[httpx2.Response, float]:
    started = time.monotonic()
    response = client.post(ENDPOINT, json=PAYLOAD, headers=HEADERS)
    return response, time.monotonic() - started


def test_budget_under_test_is_the_one_shipped(settings: Settings) -> None:
    assert settings.limits.regenerate_note_timeout_seconds == 10
    assert settings.regenerate.search_timeout_seconds == 2.5
    assert settings.regenerate.model_timeout_seconds == 5.5


def test_regenerated_note_is_sourced_from_the_search_it_ran(settings: Settings) -> None:
    llm = FakeLlmClient(REPLY)
    search = FakeSearchClient((HIT,))

    response, _ = timed_post(client_for(regenerate_note(settings, llm, search)))

    assert response.status_code == HTTPStatus.OK, response.text
    body = response.json()
    assert spec_errors("GeneratedNote", body) == []
    assert body["fields"] == {"front": "Shape of a stop sign?", "back": "Octagon"}
    assert [(source["title"], source["url"]) for source in body["sources"]] == [(HIT.title, HIT.url)]
    [query] = search.queries
    assert (query.text, query.language, query.max_results) == (
        "Road signs",
        "en",
        settings.regenerate.search_max_results,
    )
    assert "A stop sign is a red octagon." in llm.prompts[0].user


def test_slow_model_returns_a_clean_problem_well_within_ten_seconds(settings: Settings) -> None:
    llm = FakeLlmClient(hang_forever)

    response, elapsed = timed_post(client_for(regenerate_note(settings, llm, FakeSearchClient((HIT,)))))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert spec_errors("Problem", response.json()) == []
    assert (
        settings.regenerate.model_timeout_seconds <= elapsed < settings.limits.regenerate_note_timeout_seconds
    )
    assert len(llm.prompts) == 1


def test_slow_search_returns_a_clean_problem_without_calling_the_model(settings: Settings) -> None:
    llm = FakeLlmClient(REPLY)

    response, elapsed = timed_post(
        client_for(regenerate_note(settings, llm, FakeSearchClient(never_answers)))
    )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert (
        settings.regenerate.search_timeout_seconds <= elapsed < settings.regenerate.search_timeout_seconds + 1
    )
    assert llm.prompts == []


def test_transient_model_failure_is_not_retried(settings: Settings) -> None:
    llm = FakeLlmClient(LlmUnavailableError("overloaded", retry_after_seconds=7), REPLY)

    response, _ = timed_post(client_for(regenerate_note(settings, llm, FakeSearchClient((HIT,)))))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["retryAfterSeconds"] == 7
    assert len(llm.prompts) == 1


def test_search_outage_is_not_retried(settings: Settings) -> None:
    search = FakeSearchClient(SearchUnavailableError("refused"), (HIT,))

    response, _ = timed_post(client_for(regenerate_note(settings, FakeLlmClient(REPLY), search)))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert len(search.queries) == 1


@pytest.mark.anyio
async def test_concurrent_calls_to_a_hanging_model_all_end_in_time_and_then_fail_fast(
    settings: Settings,
) -> None:
    regenerate = regenerate_note(settings, FakeLlmClient(hang_forever), FakeSearchClient((HIT,)))
    request = regeneration_request(NoteType.BASIC)
    started = time.monotonic()

    outcomes = await asyncio.gather(
        *(regenerate(request, UUID(int=number, version=4)) for number in range(CONCURRENT_CALLS)),
        return_exceptions=True,
    )

    assert time.monotonic() - started < settings.limits.regenerate_note_timeout_seconds
    assert all(isinstance(outcome, UpstreamUnavailableError) for outcome in outcomes)
    fail_fast_started = time.monotonic()
    with pytest.raises(UpstreamUnavailableError):
        await regenerate(request, UUID(int=99, version=4))
    assert time.monotonic() - fail_fast_started < 1


def test_single_attempt_policy_leaves_no_room_for_a_retry() -> None:
    policy = single_attempt(5.5, base_delay_seconds=1, max_delay_seconds=20)

    assert policy.max_attempts == 1
    assert policy.deadline_seconds == policy.attempt_timeout_seconds == 5.5


def test_search_with_no_usable_page_is_no_valid_content_without_calling_the_model(settings: Settings) -> None:
    llm = FakeLlmClient(REPLY)
    unreadable = SearchHit(title="Road signs", url="https://example.com/empty", content="too short")

    response, _ = timed_post(client_for(regenerate_note(settings, llm, FakeSearchClient((unreadable,)))))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "NO_VALID_CONTENT"
    assert llm.prompts == []


@pytest.mark.parametrize(
    "model_fault",
    [LlmRejectedError("anthropic answered 400: bad request"), LlmResponseError("200 without content")],
    ids=["model rejects the request", "model answers with garbage"],
)
def test_non_transient_model_fault_is_upstream_unavailable_after_one_attempt(
    settings: Settings, model_fault: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLlmClient(model_fault, REPLY)

    response, elapsed = timed_post(client_for(regenerate_note(settings, llm, FakeSearchClient((HIT,)))))

    assert response.headers["content-type"] == "application/problem+json"
    assert spec_errors("Problem", response.json()) == []
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert response.json()["retryAfterSeconds"] == PROVIDER_FAULT_RETRY_AFTER_SECONDS
    assert response.headers["Retry-After"] == str(PROVIDER_FAULT_RETRY_AFTER_SECONDS)
    assert any(record.exc_info for record in caplog.records if record.msg == "provider_fault")
    assert elapsed < 1
    assert len(llm.prompts) == 1


@pytest.mark.parametrize(
    "search_fault",
    [SearchRejectedError("Tavily answered 400"), SearchResponseError("Tavily answered 200 without results")],
    ids=["search rejects the request", "search answers with garbage"],
)
def test_non_transient_search_fault_is_upstream_unavailable_without_calling_the_model(
    settings: Settings, search_fault: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLlmClient(REPLY)

    response, _ = timed_post(client_for(regenerate_note(settings, llm, FakeSearchClient(search_fault))))

    assert spec_errors("Problem", response.json()) == []
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert response.json()["retryAfterSeconds"] == PROVIDER_FAULT_RETRY_AFTER_SECONDS
    assert any(record.exc_info for record in caplog.records if record.msg == "provider_fault")
    assert llm.prompts == []
