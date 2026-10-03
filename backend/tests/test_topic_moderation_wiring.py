import asyncio
import os
import time
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.generations import CreateGeneration
from deckly.config import Settings
from deckly.infrastructure.llm.client import LlmReply, LlmUnavailableError
from deckly.main import API_PREFIX, build_topic_moderator
from deckly.transport import generations
from deckly.transport.error_handlers import RETRY_AFTER_HEADER, register_error_handlers
from tests.fakes import ADDRESS, FakeLlmClient, Harness, generation_request, hang_forever, model_reply, scope
from tests.test_config import settings_from_environment
from tests.transport.openapi import spec_errors

ENDPOINT = f"{API_PREFIX}/generations"
CLIENT_ID = "0b6f7c1e-4a3d-4f2e-9c8b-7a6d5e4f3a21"
HEADERS = {"Idempotency-Key": "2c9e8f7a-6b5d-4c3e-8f1a-0b9c8d7e6f5a", "X-Client-Id": CLIENT_ID}
PAYLOAD = {"topic": "Road signs", "language": "ru", "cardCount": 40}
ALLOW = model_reply({"verdict": "allow"})
BLOCK = model_reply({"verdict": "block"})
REQUEST_BUDGET_SECONDS = 0.5
SHIPPED_TOPIC_CHECK_SECONDS = 2
FAST_TOPIC_CHECK_SECONDS = "0.2"
SCHEDULING_SLACK_SECONDS = 1
CONCURRENT_REQUESTS = 5


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for name in [name for name in os.environ if name.startswith("DECKLY_")]:
        monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def settings(clean_environment: pytest.MonkeyPatch) -> Settings:
    return settings_from_environment(clean_environment)


@pytest.fixture
def fast_settings(clean_environment: pytest.MonkeyPatch) -> Settings:
    return settings_from_environment(
        clean_environment, DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS=FAST_TOPIC_CHECK_SECONDS
    )


def moderated(harness: Harness, settings: Settings, llm: FakeLlmClient) -> CreateGeneration:
    return replace(harness.create, moderator=build_topic_moderator(settings, llm))


def client_for(create: CreateGeneration) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, "https://api.example.com/problems")
    app.include_router(generations.router, prefix=API_PREFIX)
    app.state.create_generation = create
    return TestClient(app, raise_server_exceptions=False)


def timed_post(client: TestClient) -> tuple[httpx2.Response, float]:
    started = time.monotonic()
    response = client.post(ENDPOINT, json=PAYLOAD, headers=HEADERS)
    return response, time.monotonic() - started


def slow_classifier(seconds: float, answer: LlmReply) -> FakeLlmClient:
    async def answer_late() -> LlmReply:
        await asyncio.sleep(seconds)
        return answer

    return FakeLlmClient(answer_late)


def test_topic_check_budget_under_test_is_the_one_shipped(settings: Settings) -> None:
    assert settings.providers.moderation_timeout_seconds == SHIPPED_TOPIC_CHECK_SECONDS


def test_allowed_topic_is_accepted_within_the_request_budget_when_the_classifier_is_fast(
    settings: Settings,
) -> None:
    llm = FakeLlmClient(ALLOW)

    response, elapsed = timed_post(client_for(moderated(Harness(), settings, llm)))

    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    assert spec_errors("GenerationJobCreated", response.json()) == []
    assert elapsed < REQUEST_BUDGET_SECONDS
    assert len(llm.prompts) == 1


def test_rejected_topic_is_a_422_within_the_request_budget_when_the_classifier_is_fast(
    settings: Settings,
) -> None:
    harness = Harness()

    response, elapsed = timed_post(client_for(moderated(harness, settings, FakeLlmClient(BLOCK))))

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert response.json()["code"] == "TOPIC_REJECTED"
    assert elapsed < REQUEST_BUDGET_SECONDS
    assert harness.store.jobs == {}


def test_slow_classifier_that_answers_in_time_is_waited_for(fast_settings: Settings) -> None:
    timeout = fast_settings.providers.moderation_timeout_seconds
    llm = slow_classifier(timeout / 2, ALLOW)

    response, elapsed = timed_post(client_for(moderated(Harness(), fast_settings, llm)))

    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    assert timeout / 2 <= elapsed < timeout + REQUEST_BUDGET_SECONDS


def test_hanging_classifier_is_cut_off_at_the_shipped_budget_with_a_clean_503(settings: Settings) -> None:
    harness = Harness()
    timeout = settings.providers.moderation_timeout_seconds

    response, elapsed = timed_post(client_for(moderated(harness, settings, FakeLlmClient(hang_forever))))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert spec_errors("Problem", response.json()) == []
    assert RETRY_AFTER_HEADER in response.headers
    assert timeout <= elapsed < timeout + REQUEST_BUDGET_SECONDS
    assert harness.store.jobs == {}
    assert harness.queue.enqueued == []
    assert harness.quota.used_by_client[UUID(CLIENT_ID)] == 0


def test_classifier_slower_than_the_budget_is_cut_off_even_though_it_would_answer(
    fast_settings: Settings,
) -> None:
    timeout = fast_settings.providers.moderation_timeout_seconds
    llm = slow_classifier(timeout + SCHEDULING_SLACK_SECONDS, ALLOW)

    response, elapsed = timed_post(client_for(moderated(Harness(), fast_settings, llm)))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert elapsed < timeout + REQUEST_BUDGET_SECONDS


def test_transient_classifier_failure_is_not_retried_within_the_request(settings: Settings) -> None:
    llm = FakeLlmClient(LlmUnavailableError("overloaded", retry_after_seconds=7), ALLOW)

    response, _ = timed_post(client_for(moderated(Harness(), settings, llm)))

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE, response.text
    assert response.json()["retryAfterSeconds"] == 7
    assert len(llm.prompts) == 1


@pytest.mark.anyio
async def test_concurrent_requests_to_a_hanging_classifier_all_end_in_time_and_then_fail_fast(
    fast_settings: Settings,
) -> None:
    timeout = fast_settings.providers.moderation_timeout_seconds
    llm = FakeLlmClient(hang_forever)
    create = moderated(Harness(), fast_settings, llm)
    started = time.monotonic()

    outcomes = await asyncio.gather(
        *(
            create(generation_request(), scope(client=number), ADDRESS)
            for number in range(CONCURRENT_REQUESTS)
        ),
        return_exceptions=True,
    )

    assert time.monotonic() - started < timeout + REQUEST_BUDGET_SECONDS
    assert all(isinstance(outcome, UpstreamUnavailableError) for outcome in outcomes)
    calls_before = len(llm.prompts)
    fail_fast_started = time.monotonic()
    with pytest.raises(UpstreamUnavailableError):
        await create(generation_request(), scope(client=99), ADDRESS)
    assert time.monotonic() - fail_fast_started < timeout
    assert len(llm.prompts) == calls_before
