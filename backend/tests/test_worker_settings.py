from typing import get_args

import pytest
from arq.worker import create_worker
from pydantic import AnyHttpUrl, SecretStr

from deckly.config import ModelProvider, ProviderSettings
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.worker.settings import LLM_CLIENTS, WorkerSettings, build_llm_client

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
        search_api_key=SecretStr("key"),
        search_timeout_seconds=5,
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


async def test_worker_cancels_the_task_of_a_job_aborted_through_the_queue() -> None:
    worker = create_worker(WorkerSettings, handle_signals=False)

    assert worker.allow_abort_jobs is True
