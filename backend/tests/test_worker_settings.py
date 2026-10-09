import logging
from datetime import datetime, timedelta
from typing import get_args
from uuid import UUID, uuid4

import pytest
from arq.worker import create_worker
from pydantic import AnyHttpUrl, SecretStr

from deckly.application.housekeeping import (
    EnforceJobRetention,
    RetentionPolicy,
    StalenessPolicy,
    SweepStaleJobs,
)
from deckly.config import ModelProvider, ProviderSettings, SweepSettings
from deckly.domain.job import GenerationJob, JobStatus
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.llm.anthropic_client import AnthropicLlmClient
from deckly.infrastructure.llm.deepseek_client import DeepSeekLlmClient
from deckly.infrastructure.llm.gemini_client import GeminiLlmClient
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.media.commons_client import CommonsImageSearchClient
from deckly.infrastructure.media.fetcher import CommonsMediaFetcher, MediaLimits
from deckly.infrastructure.moderation.image_judge import LlmCandidateJudge
from deckly.infrastructure.resilience import ResilientCaller
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from deckly.worker.settings import (
    ENFORCE_JOB_RETENTION_KEY,
    HOUSEKEEPING_TASK,
    LLM_CLIENT_KEY,
    LLM_CLIENTS,
    MEDIA_FETCHER_KEY,
    MEDIA_JUDGE_LLM_CLIENT_KEY,
    MODERATION_LLM_CLIENT_KEY,
    SOURCE_RETRIEVER_KEY,
    SWEEP_STALE_JOBS_KEY,
    WorkerContext,
    WorkerSettings,
    build_llm_client,
    build_media_fetcher,
    build_media_judge_llm_client,
    build_moderation_llm_client,
    build_source_retriever,
    housekeeping_cron_jobs,
    moderation_llm_client,
    run_housekeeping,
    shutdown,
)
from tests.domain.builders import T0
from tests.fakes import (
    MEDIA_LIMITS,
    SEARCH_POLICY,
    FakeCandidateJudge,
    FakeImageSearchClient,
    FakeLlmClient,
    FakeSearchClient,
    InMemoryJobHousekeeping,
    InMemoryJobStore,
    ManualTime,
    fresh_probe,
    generation_request,
    job_id,
    scope,
)

pytestmark = pytest.mark.anyio

PROVIDER_CLIENTS: dict[ModelProvider, type[AnthropicLlmClient | DeepSeekLlmClient | GeminiLlmClient]] = {
    "anthropic": AnthropicLlmClient,
    "deepseek": DeepSeekLlmClient,
    "gemini": GeminiLlmClient,
}


def provider_settings(provider: ModelProvider) -> ProviderSettings:
    return ProviderSettings(
        model_provider=provider,
        model_base_url=AnyHttpUrl("https://api.example.com"),
        model_api_key=SecretStr("key"),
        model_name="model",
        model_max_output_tokens=1000,
        model_timeout_seconds=10,
        model_deadline_seconds=20,
        model_max_attempts=2,
        model_retry_base_delay_seconds=1,
        model_retry_max_delay_seconds=2,
        model_circuit_failure_threshold=3,
        model_circuit_reset_seconds=5,
        search_base_url=AnyHttpUrl("https://api.tavily.com"),
        search_api_key=SecretStr("key"),
        search_timeout_seconds=5,
        search_deadline_seconds=10,
        search_max_attempts=2,
        search_retry_base_delay_seconds=1,
        search_retry_max_delay_seconds=2,
        search_circuit_failure_threshold=3,
        search_circuit_reset_seconds=5,
        search_max_results=6,
        search_max_source_characters=8000,
        media_base_url=AnyHttpUrl("https://commons.wikimedia.org"),
        media_user_agent="Deckly/0.1 (https://example.com/contact)",
        media_timeout_seconds=5,
        media_deadline_seconds=15,
        media_max_attempts=2,
        media_retry_base_delay_seconds=1,
        media_retry_max_delay_seconds=2,
        media_circuit_failure_threshold=3,
        media_circuit_reset_seconds=5,
        media_max_images=12,
        media_candidates_per_query=7,
        media_thumbnail_width=960,
        media_max_concurrency=3,
        media_judge_timeout_seconds=4,
        media_judge_deadline_seconds=8,
        media_judge_max_attempts=2,
        media_judged_candidates_per_note=3,
        moderation_model_name="moderation-model",
        moderation_max_output_tokens=500,
        moderation_timeout_seconds=2,
        moderation_filter_timeout_seconds=5,
        moderation_filter_deadline_seconds=10,
        moderation_filter_max_attempts=2,
    )


def test_every_configurable_model_provider_has_a_client() -> None:
    assert set(LLM_CLIENTS) == set(get_args(ModelProvider))


@pytest.mark.parametrize("provider", get_args(ModelProvider))
async def test_llm_client_is_built_behind_the_resilience_layer_for_every_provider(
    provider: ModelProvider,
) -> None:
    client = build_llm_client(provider_settings(provider), probe=fresh_probe())

    assert isinstance(client, ResilientLlmClient)
    await client.aclose()


@pytest.mark.parametrize("provider", get_args(ModelProvider))
async def test_moderation_client_is_built_for_every_provider_behind_its_own_resilience_layer(
    provider: ModelProvider,
) -> None:
    providers = provider_settings(provider)
    client = build_moderation_llm_client(providers, probe=fresh_probe())
    plain = moderation_llm_client(providers, providers.moderation_timeout_seconds)

    assert isinstance(client, ResilientLlmClient)
    assert isinstance(plain, PROVIDER_CLIENTS[provider])
    await client.aclose()
    await plain.aclose()


async def test_source_retriever_searches_tavily_behind_the_resilience_layer_with_the_configured_cap() -> None:
    retriever = build_source_retriever(provider_settings("anthropic"), probe=fresh_probe())

    assert isinstance(retriever, WebSourceRetriever)
    assert isinstance(retriever.client, TavilySearchClient)
    assert retriever.max_results == 6
    await retriever.aclose()


async def test_media_fetcher_searches_commons_behind_the_resilience_layer_with_the_configured_limits() -> (
    None
):
    judge_llm = FakeLlmClient()
    fetcher = build_media_fetcher(provider_settings("anthropic"), fresh_probe(), judge_llm)

    assert isinstance(fetcher, CommonsMediaFetcher)
    assert isinstance(fetcher.client, CommonsImageSearchClient)
    assert isinstance(fetcher.caller, ResilientCaller)
    assert fetcher.judge == LlmCandidateJudge(llm=judge_llm)
    assert fetcher.limits == MediaLimits(
        max_images=12,
        candidates_per_query=7,
        thumbnail_width=960,
        max_concurrency=3,
        deadline_seconds=15,
        judged_candidates_per_note=3,
    )
    await fetcher.aclose()


@pytest.mark.parametrize("provider", get_args(ModelProvider))
async def test_media_judge_client_is_built_for_every_provider_behind_its_own_resilience_layer(
    provider: ModelProvider,
) -> None:
    client = build_media_judge_llm_client(provider_settings(provider), probe=fresh_probe())

    assert isinstance(client, ResilientLlmClient)
    await client.aclose()


async def test_worker_cancels_the_task_of_a_job_aborted_through_the_queue() -> None:
    worker = create_worker(WorkerSettings, handle_signals=False)

    assert worker.allow_abort_jobs is True


class FailingToCloseLlm(FakeLlmClient):
    async def aclose(self) -> None:
        message = "connection pool already broken"
        raise RuntimeError(message)


async def test_shutdown_closes_the_search_and_image_clients_even_when_closing_the_model_client_fails() -> (
    None
):
    time = ManualTime()
    search = FakeSearchClient(())
    images = FakeImageSearchClient(())
    caller = ResilientCaller(SEARCH_POLICY, time.breaker(), time.runtime(), probe=fresh_probe())
    ctx: WorkerContext = {
        LLM_CLIENT_KEY: ResilientLlmClient(FailingToCloseLlm(), caller, 1000),
        SOURCE_RETRIEVER_KEY: WebSourceRetriever(client=search, caller=caller, clock=utc_now, max_results=6),
        MEDIA_FETCHER_KEY: CommonsMediaFetcher(
            client=images, caller=caller, judge=FakeCandidateJudge(), new_id=uuid4, limits=MEDIA_LIMITS
        ),
    }

    with pytest.raises(RuntimeError):
        await shutdown(ctx)

    assert search.closed
    assert images.closed


async def test_shutdown_closes_the_moderation_clients_even_when_closing_the_model_client_fails() -> None:
    time = ManualTime()
    caller = ResilientCaller(SEARCH_POLICY, time.breaker(), time.runtime(), probe=fresh_probe())
    moderation = FakeLlmClient()
    media_judge = FakeLlmClient()
    ctx: WorkerContext = {
        LLM_CLIENT_KEY: ResilientLlmClient(FailingToCloseLlm(), caller, 1000),
        MODERATION_LLM_CLIENT_KEY: ResilientLlmClient(moderation, caller, 1000),
        MEDIA_JUDGE_LLM_CLIENT_KEY: ResilientLlmClient(media_judge, caller, 1000),
    }

    with pytest.raises(RuntimeError):
        await shutdown(ctx)

    assert moderation.closed
    assert media_judge.closed


def sweep_settings(interval_minutes: int) -> SweepSettings:
    return SweepSettings(
        interval_minutes=interval_minutes,
        running_stale_after_seconds=900,
        queued_stale_after_seconds=3600,
        batch_size=500,
    )


@pytest.mark.parametrize(
    ("interval_minutes", "minutes"),
    [(5, set(range(0, 60, 5))), (15, {0, 15, 30, 45}), (60, {0}), (1, set(range(60)))],
)
def test_housekeeping_runs_on_every_interval_boundary_of_the_hour_and_never_overlaps_itself(
    interval_minutes: int, minutes: set[int]
) -> None:
    [job] = housekeeping_cron_jobs(sweep_settings(interval_minutes))

    assert job.name == HOUSEKEEPING_TASK
    assert job.minute == minutes
    assert job.second == 0
    assert job.timeout_s == interval_minutes * 60
    assert job.unique is True
    assert job.run_at_startup is False


async def test_housekeeping_is_registered_with_the_arq_worker_next_to_the_generation_task() -> None:
    worker = create_worker(
        WorkerSettings, cron_jobs=housekeeping_cron_jobs(sweep_settings(5)), handle_signals=False
    )

    assert HOUSEKEEPING_TASK in worker.functions
    assert [cron_job.name for cron_job in worker.cron_jobs] == [HOUSEKEEPING_TASK]


class UnreachableHousekeeping(InMemoryJobHousekeeping):
    async def stale(self, status: JobStatus, updated_before: datetime, limit: int) -> tuple[UUID, ...]:
        del status, updated_before, limit
        message = "database unreachable"
        raise ConnectionError(message)


async def test_housekeeping_still_enforces_retention_when_the_stale_sweep_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryJobStore()
    await store.add(GenerationJob.queue(job_id(1), T0), generation_request(), scope())
    now = T0 + timedelta(days=2)
    ctx: WorkerContext = {
        SWEEP_STALE_JOBS_KEY: SweepStaleJobs(
            store=store,
            housekeeping=UnreachableHousekeeping(store),
            policy=StalenessPolicy(running_after=timedelta(minutes=15), queued_after=timedelta(hours=1)),
            batch_size=10,
            clock=lambda: now,
        ),
        ENFORCE_JOB_RETENTION_KEY: EnforceJobRetention(
            housekeeping=InMemoryJobHousekeeping(store),
            policy=RetentionPolicy(idempotency_key_ttl=timedelta(days=1), job_retention=timedelta(days=1)),
            batch_size=10,
            clock=lambda: now,
        ),
    }

    with caplog.at_level(logging.ERROR, logger="deckly.worker.settings"):
        await run_housekeeping(ctx)

    assert await store.find(scope()) is None
    assert [(record.message, record.__dict__["step"]) for record in caplog.records] == [
        ("housekeeping_step_failed", SWEEP_STALE_JOBS_KEY)
    ]
