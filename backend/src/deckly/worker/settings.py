import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from secrets import SystemRandom
from uuid import UUID, uuid4

from arq.cron import CronJob
from arq.typing import StartupShutdown, WorkerCoroutine
from arq.worker import Function, func
from sqlalchemy.ext.asyncio import AsyncEngine

from deckly.application.pipeline import RunGeneration
from deckly.config import ModelProvider, ProviderSettings, Settings
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.database import create_engine, create_session_factory, verify_connection
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.infrastructure.llm.anthropic_client import AnthropicLlmClient
from deckly.infrastructure.llm.client import LlmClient, LlmEndpoint
from deckly.infrastructure.llm.deepseek_client import DeepSeekLlmClient
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.providers import (
    UnimplementedMediaFetcher,
    UnimplementedSourceParser,
    UnimplementedSourceRetriever,
)
from deckly.infrastructure.queue import GENERATION_TASK
from deckly.infrastructure.resilience import CircuitBreaker, ResilientCaller, RetryPolicy, RetryRuntime

SETTINGS_KEY = "settings"
ENGINE_KEY = "engine"
LLM_CLIENT_KEY = "llm_client"
RUN_GENERATION_KEY = "run_generation_use_case"

LLM_CLIENTS: Mapping[ModelProvider, Callable[[LlmEndpoint], LlmClient]] = {
    "anthropic": AnthropicLlmClient,
    "deepseek": DeepSeekLlmClient,
}

type WorkerContext = dict[str, object]


def from_context[T](ctx: WorkerContext, key: str, kind: type[T]) -> T:
    value = ctx.get(key)
    if not isinstance(value, kind):
        message = f"{key} is not wired into the worker context"
        raise TypeError(message)
    return value


def build_llm_client(providers: ProviderSettings) -> ResilientLlmClient:
    endpoint = LlmEndpoint(
        base_url=str(providers.model_base_url),
        api_key=providers.model_api_key.get_secret_value(),
        model=providers.model_name,
        max_output_tokens=providers.model_max_output_tokens,
        timeout_seconds=providers.model_timeout_seconds,
    )
    policy = RetryPolicy(
        max_attempts=providers.model_max_attempts,
        attempt_timeout_seconds=providers.model_timeout_seconds,
        deadline_seconds=providers.model_deadline_seconds,
        base_delay_seconds=providers.model_retry_base_delay_seconds,
        max_delay_seconds=providers.model_retry_max_delay_seconds,
    )
    breaker = CircuitBreaker(
        failure_threshold=providers.model_circuit_failure_threshold,
        reset_seconds=providers.model_circuit_reset_seconds,
        clock=time.monotonic,
    )
    runtime = RetryRuntime(clock=time.monotonic, sleep=asyncio.sleep, jitter=SystemRandom().random)
    client = LLM_CLIENTS[providers.model_provider](endpoint)
    return ResilientLlmClient(client, ResilientCaller(policy, breaker, runtime))


async def run_generation(ctx: WorkerContext, job_id: str) -> None:
    await from_context(ctx, RUN_GENERATION_KEY, RunGeneration)(UUID(job_id))


async def startup(ctx: WorkerContext) -> None:
    settings = from_context(ctx, SETTINGS_KEY, Settings)
    engine = create_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout_seconds=settings.database.pool_timeout_seconds,
    )
    ctx[ENGINE_KEY] = engine
    await verify_connection(engine)
    llm = build_llm_client(settings.providers)
    ctx[LLM_CLIENT_KEY] = llm
    ctx[RUN_GENERATION_KEY] = RunGeneration(
        store=PostgresJobStore(create_session_factory(engine)),
        retriever=UnimplementedSourceRetriever(),
        parser=UnimplementedSourceParser(),
        generator=LlmCardGenerator(llm=llm, new_id=uuid4, handlers=NOTE_TYPE_HANDLERS),
        media=UnimplementedMediaFetcher(),
        clock=utc_now,
    )


async def shutdown(ctx: WorkerContext) -> None:
    llm = ctx.get(LLM_CLIENT_KEY)
    engine = ctx.get(ENGINE_KEY)
    try:
        if isinstance(llm, ResilientLlmClient):
            await llm.aclose()
    finally:
        if isinstance(engine, AsyncEngine):
            await engine.dispose()


class WorkerSettings:
    functions: Sequence[WorkerCoroutine | Function] = (
        func(run_generation, name=GENERATION_TASK, keep_result=0),
    )
    cron_jobs: Sequence[CronJob] | None = None
    on_startup: StartupShutdown | None = startup
    on_shutdown: StartupShutdown | None = shutdown
