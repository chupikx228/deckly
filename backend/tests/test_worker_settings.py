from typing import get_args

import pytest
from arq.worker import create_worker
from pydantic import AnyHttpUrl, SecretStr

from deckly.config import ModelProvider, ProviderSettings
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.resilience import ResilientCaller
from deckly.infrastructure.search.retriever import WebSourceRetriever
from deckly.infrastructure.search.tavily_client import TavilySearchClient
from deckly.worker.settings import (
    LLM_CLIENT_KEY,
    LLM_CLIENTS,
    SOURCE_RETRIEVER_KEY,
    WorkerContext,
    WorkerSettings,
    build_llm_client,
    build_source_retriever,
    shutdown,
)
from tests.fakes import SEARCH_POLICY, FakeLlmClient, FakeSearchClient, ManualTime

pytestmark = pytest.mark.anyio


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
    )


def test_every_configurable_model_provider_has_a_client() -> None:
    assert set(LLM_CLIENTS) == set(get_args(ModelProvider))


@pytest.mark.parametrize("provider", get_args(ModelProvider))
async def test_llm_client_is_built_behind_the_resilience_layer_for_every_provider(
    provider: ModelProvider,
) -> None:
    client = build_llm_client(provider_settings(provider))

    assert isinstance(client, ResilientLlmClient)
    await client.aclose()


async def test_source_retriever_searches_tavily_behind_the_resilience_layer_with_the_configured_cap() -> None:
    retriever = build_source_retriever(provider_settings("anthropic"))

    assert isinstance(retriever, WebSourceRetriever)
    assert isinstance(retriever.client, TavilySearchClient)
    assert retriever.max_results == 6
    await retriever.aclose()


async def test_worker_cancels_the_task_of_a_job_aborted_through_the_queue() -> None:
    worker = create_worker(WorkerSettings, handle_signals=False)

    assert worker.allow_abort_jobs is True


class FailingToCloseLlm(FakeLlmClient):
    async def aclose(self) -> None:
        message = "connection pool already broken"
        raise RuntimeError(message)


async def test_shutdown_closes_the_search_client_even_when_closing_the_model_client_fails() -> None:
    time = ManualTime()
    search = FakeSearchClient(())
    caller = ResilientCaller(SEARCH_POLICY, time.breaker(), time.runtime())
    ctx: WorkerContext = {
        LLM_CLIENT_KEY: ResilientLlmClient(FailingToCloseLlm(), caller, 1000),
        SOURCE_RETRIEVER_KEY: WebSourceRetriever(client=search, caller=caller, clock=utc_now, max_results=6),
    }

    with pytest.raises(RuntimeError):
        await shutdown(ctx)

    assert search.closed
