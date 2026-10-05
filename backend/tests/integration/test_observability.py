import io
import json
import traceback
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx2
import pytest
from arq.connections import ArqRedis
from arq.jobs import Job
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.exceptions import RateLimitedError
from deckly.application.pipeline import RunGeneration
from deckly.config import Settings
from deckly.domain.job import STAGE_ORDER, FailureCode, JobStage
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.llm.client import REDACTED
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.media.commons_client import CommonsImageSearchClient
from deckly.infrastructure.media.fetcher import CommonsMediaFetcher
from deckly.infrastructure.moderation.moderator import LlmContentModerator
from deckly.infrastructure.observability.runtime import Observability, create_observability
from deckly.infrastructure.rate_limit import RedisRegenerationLimiter, RegenerationWindow
from deckly.infrastructure.resilience import ProviderOperation, RetryPolicy
from deckly.infrastructure.result_cache import RedisResultCache, ResultCacheLimits, cache_key
from deckly.infrastructure.search.client import SearchEndpoint
from deckly.infrastructure.search.parser import CleaningSourceParser
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from deckly.main import API_PREFIX, create_app
from deckly.worker.settings import RUN_GENERATION_KEY, resilient_caller, run_generation
from tests.fakes import (
    MEDIA_ENDPOINT,
    MEDIA_LIMITS,
    TEST_SERVICE_NAME,
    FakeLlmClient,
    commons_page,
    commons_results,
    model_reply,
    tavily_result,
)
from tests.integration.conftest import (
    TEST_PEER_ADDRESSES,
    Cleanup,
    purge_quota,
    with_generation_limits,
)
from tests.logs import JsonLogs, LogLine, captured_json_logs

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ENDPOINT = f"{API_PREFIX}/generations"
CARD_COUNT = 5
MAX_OUTPUT_TOKENS = 16_000
FAST_POLICY = RetryPolicy(
    max_attempts=3,
    attempt_timeout_seconds=5,
    deadline_seconds=30,
    base_delay_seconds=0.01,
    max_delay_seconds=0.01,
)
PAGE_TEXT = (
    "A red octagon with the word STOP means come to a complete stop before the line. "
    "An inverted triangle with a red border means yield to traffic on the main road. "
    "A police officer directing traffic overrides every sign and signal at the junction."
)
DETAIL_FIELDS = ("admission", "stage", "operation", "result")
ILLUSTRATED_NOTES = 2

type Connections = tuple[async_sessionmaker[AsyncSession], ArqRedis]


@dataclass(frozen=True, slots=True)
class Canaries:
    topic: str
    content: str
    key: str

    def present_in(self, logged: str) -> list[str]:
        return [canary for canary in (self.topic, self.content, self.key) if canary in logged]


@dataclass(frozen=True, slots=True)
class Scenario:
    canaries: Canaries
    search_status: HTTPStatus


@dataclass(frozen=True, slots=True)
class Lifecycle:
    job_id: UUID
    client_id: UUID
    logs: JsonLogs
    spans: str
    worker: Observability
    scraped: str


def fresh_canaries() -> Canaries:
    marker = uuid4().hex
    return Canaries(
        topic=f"Canarytopic{marker} road signs",
        content=f"canarycontent{marker}",
        key=f"tvly-canarykey{marker}",
    )


def generated_cards(content: str) -> dict[str, object]:
    return {
        "deck": {"title": "Road signs"},
        "notes": [
            {
                "noteType": "basic",
                "fields": {"front": f"What does a red octagon mean {content}?", "back": "Stop"},
                "sources": [1],
                "image": f"stop sign {content}",
            },
            {
                "noteType": "basic",
                "fields": {"front": f"What does an inverted triangle mean {content}?", "back": "Yield"},
                "sources": [1],
                "image": f"yield sign {content}",
            },
            {
                "noteType": "basic",
                "fields": {"front": f"Who overrides every sign {content}?", "back": "A police officer"},
                "sources": [1],
            },
        ],
    }


def echoing_search(canaries: Canaries, status: HTTPStatus) -> Callable[[httpx2.Request], httpx2.Response]:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        query = str(json.loads(request.content)["query"])
        calls.append(query)
        if len(calls) == 1:
            detail = f"could not search for '{query}' with key {canaries.key}"
            return httpx2.Response(status, json={"detail": {"error": detail}})
        return httpx2.Response(
            HTTPStatus.OK,
            json={"results": [tavily_result("Road signs", "https://example.com/signs", PAGE_TEXT)]},
        )

    return handler


def commons(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(HTTPStatus.OK, json=commons_results(commons_page(1)))


def resilient_llm(
    worker: Observability, operation: ProviderOperation, reply: dict[str, object]
) -> ResilientLlmClient:
    caller = resilient_caller(FAST_POLICY, worker.probe(operation), failure_threshold=5, reset_seconds=30)
    return ResilientLlmClient(FakeLlmClient(model_reply(reply)), caller, MAX_OUTPUT_TOKENS)


async def worker_pipeline(
    worker: Observability,
    stack: AsyncExitStack,
    connections: Connections,
    scenario: Scenario,
) -> RunGeneration:
    session_factory, pool = connections
    canaries = scenario.canaries
    retriever = WebSourceRetriever(
        client=TavilySearchClient(
            SearchEndpoint(base_url="https://api.tavily.test", api_key=canaries.key, timeout_seconds=5),
            httpx2.MockTransport(echoing_search(canaries, scenario.search_status)),
        ),
        caller=resilient_caller(
            FAST_POLICY, worker.probe(ProviderOperation.WEB_SEARCH), failure_threshold=5, reset_seconds=30
        ),
        clock=utc_now,
        max_results=6,
    )
    stack.push_async_callback(retriever.aclose)
    media = CommonsMediaFetcher(
        client=CommonsImageSearchClient(MEDIA_ENDPOINT, httpx2.MockTransport(commons)),
        caller=resilient_caller(
            FAST_POLICY, worker.probe(ProviderOperation.IMAGE_SEARCH), failure_threshold=5, reset_seconds=30
        ),
        new_id=uuid4,
        limits=MEDIA_LIMITS,
    )
    stack.push_async_callback(media.aclose)
    return RunGeneration(
        store=PostgresJobStore(session_factory),
        cache=RedisResultCache(
            pool, ResultCacheLimits(ttl_seconds=60, command_timeout_seconds=2), worker.metrics
        ),
        retriever=retriever,
        parser=CleaningSourceParser(max_characters=4000),
        generator=LlmCardGenerator(
            llm=resilient_llm(worker, ProviderOperation.CARD_GENERATION, generated_cards(canaries.content)),
            new_id=uuid4,
            handlers=NOTE_TYPE_HANDLERS,
        ),
        moderator=LlmContentModerator(
            llm=resilient_llm(
                worker,
                ProviderOperation.CONTENT_MODERATION,
                {"deck": "allow", "notes": {"1": "allow", "2": "allow", "3": "allow"}},
            )
        ),
        media=media,
        clock=utc_now,
        telemetry=worker.generation(),
    )


def timeline(lines: list[LogLine]) -> list[tuple[object, object]]:
    return [
        (line["message"], next((line[field] for field in DETAIL_FIELDS if line.get(field) is not None), None))
        for line in lines
    ]


async def run_lifecycle(
    settings: Settings,
    cleanup: Cleanup,
    connections: Connections,
    status: HTTPStatus,
) -> tuple[Lifecycle, Canaries]:
    canaries = fresh_canaries()
    session_factory, pool = connections
    client_id = cleanup.scope().client_id
    headers = {"X-Client-Id": str(client_id)}
    spans = io.StringIO()
    worker = create_observability(TEST_SERVICE_NAME, "console", spans)
    app = create_app(settings)
    with captured_json_logs() as logs, TestClient(app) as client:
        response = client.post(
            ENDPOINT,
            json={"topic": canaries.topic, "language": "en", "cardCount": CARD_COUNT, "includeImages": True},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == HTTPStatus.ACCEPTED, response.text
        job_id = UUID(str(response.json()["jobId"]))
        cleanup.job_ids.add(job_id)
        info = await Job(str(job_id), pool).info()
        assert info is not None
        job_argument, trace_context = info.args
        async with AsyncExitStack() as stack:
            run = await worker_pipeline(worker, stack, connections, Scenario(canaries, status))
            await run_generation({RUN_GENERATION_KEY: run}, str(job_argument), trace_context)
        assert client.get(f"{ENDPOINT}/{job_id}", headers=headers).status_code == HTTPStatus.OK
        scraped = client.get("/metrics").text
    stored = await PostgresJobStore(session_factory).get_stored(job_id)
    assert stored is not None
    await pool.delete(cache_key(stored.request.fingerprint()))
    worker.shutdown()
    return (
        Lifecycle(
            job_id=job_id,
            client_id=client_id,
            logs=logs,
            spans=spans.getvalue(),
            worker=worker,
            scraped=scraped,
        ),
        canaries,
    )


@pytest.fixture
def connections(session_factory: async_sessionmaker[AsyncSession], queue_pool: ArqRedis) -> Connections:
    return session_factory, queue_pool


def sample(worker: Observability, name: str, **labels: str) -> float | None:
    return worker.metrics.registry.get_sample_value(name, labels)


@pytest.mark.usefixtures("restored_logging")
async def test_one_jobs_whole_lifecycle_is_reconstructable_from_the_lines_carrying_its_job_id(
    settings: Settings,
    cleanup: Cleanup,
    connections: Connections,
) -> None:
    lifecycle, _ = await run_lifecycle(settings, cleanup, connections, HTTPStatus.SERVICE_UNAVAILABLE)

    job_lines = lifecycle.logs.for_field("job_id", str(lifecycle.job_id))
    assert timeline(job_lines) == [
        ("generation_admitted", "queued"),
        ("generation_started", JobStage.PLANNING),
        ("generation_cache_checked", "miss"),
        ("generation_stage_entered", JobStage.RETRIEVING_SOURCES),
        ("provider_call_finished", ProviderOperation.WEB_SEARCH),
        ("provider_call_retrying", ProviderOperation.WEB_SEARCH),
        ("provider_call_finished", ProviderOperation.WEB_SEARCH),
        ("sources_retrieved", None),
        ("generation_stage_entered", JobStage.PARSING_SOURCES),
        ("sources_parsed", None),
        ("generation_stage_entered", JobStage.GENERATING_CARDS),
        ("provider_call_finished", ProviderOperation.CARD_GENERATION),
        ("card_generation_finished", None),
        ("provider_call_finished", ProviderOperation.CONTENT_MODERATION),
        ("content_screened", None),
        ("generation_stage_entered", JobStage.FETCHING_MEDIA),
        *[("provider_call_finished", ProviderOperation.IMAGE_SEARCH)] * ILLUSTRATED_NOTES,
        ("media_fetched", None),
        ("media_attached", None),
        ("generation_stage_entered", JobStage.FINALIZING),
        ("generation_succeeded", None),
    ]
    [retried, *_] = lifecycle.logs.named("provider_call_finished")
    assert (retried["outcome"], retried["error"]) == ("transient", "SearchUnavailableError")
    [admitted] = lifecycle.logs.named("generation_admitted")
    post, poll = lifecycle.logs.named("http_request_finished")
    assert (post["route"], post["status"]) == (ENDPOINT, HTTPStatus.ACCEPTED)
    assert (poll["route"], poll["status"]) == (f"{ENDPOINT}/{{job_id}}", HTTPStatus.OK)
    assert post["client_id"] == poll["client_id"] == admitted["client_id"] == str(lifecycle.client_id)
    assert {line["trace_id"] for line in job_lines} == {post["trace_id"]}


@pytest.mark.usefixtures("restored_logging")
async def test_every_metric_the_ticket_lists_is_emitted_by_a_local_run(
    settings: Settings,
    cleanup: Cleanup,
    connections: Connections,
) -> None:
    lifecycle, _ = await run_lifecycle(settings, cleanup, connections, HTTPStatus.SERVICE_UNAVAILABLE)
    worker = lifecycle.worker

    assert 'deckly_generation_jobs_admitted_total{outcome="queued"} 1.0' in lifecycle.scraped
    [depth] = [
        line
        for line in lifecycle.scraped.splitlines()
        if line.startswith('deckly_generation_queue_depth{queue="generation"} ')
    ]
    assert float(depth.split()[1]) >= 1
    assert sample(worker, "deckly_generation_jobs_finished_total", outcome="succeeded", failure_code="") == 1
    assert sample(worker, "deckly_generation_job_duration_seconds_count", outcome="succeeded") == 1
    for stage in STAGE_ORDER:
        assert (
            sample(worker, "deckly_generation_stage_duration_seconds_count", stage=stage, outcome="ok") == 1
        )
    for operation, calls in {
        ProviderOperation.WEB_SEARCH: 1,
        ProviderOperation.CARD_GENERATION: 1,
        ProviderOperation.CONTENT_MODERATION: 1,
        ProviderOperation.IMAGE_SEARCH: ILLUSTRATED_NOTES,
    }.items():
        assert (
            sample(worker, "deckly_provider_call_duration_seconds_count", operation=operation, outcome="ok")
            == calls
        )
    assert (
        sample(
            worker,
            "deckly_provider_call_duration_seconds_count",
            operation=ProviderOperation.WEB_SEARCH,
            outcome="transient",
        )
        == 1
    )
    assert sample(worker, "deckly_provider_call_retries_total", operation=ProviderOperation.WEB_SEARCH) == 1
    assert sample(worker, "deckly_generation_cache_lookups_total", result="miss") == 1


@pytest.mark.usefixtures("restored_logging")
@pytest.mark.parametrize("status", [HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.BAD_REQUEST])
async def test_no_secret_topic_or_generated_content_reaches_the_logs_or_spans(
    settings: Settings,
    cleanup: Cleanup,
    connections: Connections,
    status: HTTPStatus,
) -> None:
    lifecycle, canaries = await run_lifecycle(settings, cleanup, connections, status)

    assert canaries.present_in(lifecycle.logs.text) == []
    assert canaries.present_in(lifecycle.spans) == []
    if status is HTTPStatus.BAD_REQUEST:
        [failed] = lifecycle.logs.named("generation_failed")
        assert failed["failure_code"] == FailureCode.GENERATION_FAILED
        assert f"could not search for '{REDACTED}' with key {REDACTED}" in str(failed["exception"])


@pytest.mark.usefixtures("restored_logging")
@pytest.mark.parametrize(
    "limits", [(1, 100, "generation_client"), (100, 1, "generation_address")], ids=["client", "address"]
)
async def test_a_rate_limit_rejection_is_counted_under_the_limit_that_tripped(
    settings: Settings, cleanup: Cleanup, queue_pool: ArqRedis, limits: tuple[int, int, str]
) -> None:
    per_client, per_address, tripped = limits
    await purge_quota(queue_pool, (), TEST_PEER_ADDRESSES)
    app = create_app(with_generation_limits(settings, per_client=per_client, per_address=per_address))
    client_ids = [cleanup.scope().client_id, cleanup.scope().client_id]

    with captured_json_logs() as logs, TestClient(app) as client:
        statuses = []
        for client_id in [client_ids[0], client_ids[0] if tripped == "generation_client" else client_ids[1]]:
            response = client.post(
                ENDPOINT,
                json={"topic": "Road signs", "language": "en", "cardCount": CARD_COUNT},
                headers={"X-Client-Id": str(client_id), "Idempotency-Key": str(uuid4())},
            )
            statuses.append(response.status_code)
            if response.status_code == HTTPStatus.ACCEPTED:
                cleanup.job_ids.add(UUID(str(response.json()["jobId"])))
        scraped = client.get("/metrics").text

    assert statuses == [HTTPStatus.ACCEPTED, HTTPStatus.TOO_MANY_REQUESTS]
    assert f'deckly_rate_limit_rejections_total{{limit="{tripped}"}} 1.0' in scraped
    [rejected] = logs.named("rate_limit_rejected")
    assert rejected["limit"] == tripped
    assert rejected["client_id"] == str(client_ids[0] if tripped == "generation_client" else client_ids[1])


async def test_a_note_regeneration_rejection_is_counted(queue_pool: ArqRedis) -> None:
    worker = create_observability(TEST_SERVICE_NAME, "none", io.StringIO())
    limiter = RedisRegenerationLimiter(
        queue_pool, RegenerationWindow(limit=1, window_seconds=60, command_timeout_seconds=2), worker.metrics
    )
    client_id, now = uuid4(), datetime.now(UTC)

    await limiter.acquire(client_id, now)
    with pytest.raises(RateLimitedError):
        await limiter.acquire(client_id, now)

    assert sample(worker, "deckly_rate_limit_rejections_total", limit="note_regeneration") == 1


async def test_database_errors_never_carry_the_values_that_were_bound(settings: Settings) -> None:
    canary = fresh_canaries().topic
    engine = create_engine(str(settings.database.url), pool_size=1, max_overflow=0, pool_timeout_seconds=10)
    try:
        with pytest.raises(DBAPIError) as raised:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT CAST(:topic AS text), 1 / 0"), {"topic": canary})
    finally:
        await engine.dispose()

    logged = "".join(traceback.format_exception(raised.value))
    assert canary not in logged
    assert "hide_parameters" in logged
