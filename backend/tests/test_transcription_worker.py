import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from arq.connections import ArqRedis
from arq.constants import default_queue_name

from deckly.config import TRANSCRIPTION_WORKER_MAX_JOBS, ObservabilitySettings, Settings
from deckly.infrastructure.queue import GENERATION_TASK, TRANSCRIPTION_QUEUE_NAME
from deckly.infrastructure.transcription import model
from deckly.infrastructure.transcription.self_hosted import SelfHostedTranscriptionClient
from deckly.worker.main import (
    build_transcription_worker,
    build_worker,
    generation_metrics_port,
    transcription_metrics_port,
)
from deckly.worker.settings import (
    ENGINE_KEY,
    OBSERVABILITY_KEY,
    SETTINGS_KEY,
    TRANSCRIPTION_CLIENT_KEY,
    WorkerContext,
    build_recording_transcriber,
    build_transcription_client,
    shutdown,
    transcription_startup,
)
from tests.fakes import fresh_metrics, fresh_observability
from tests.test_config import settings_from_environment

pytestmark = pytest.mark.anyio


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.chdir(tmp_path)
    for name in [name for name in os.environ if name.startswith("DECKLY_")]:
        monkeypatch.delenv(name)
    return settings_from_environment(monkeypatch)


def context(settings: Settings) -> WorkerContext:
    return {SETTINGS_KEY: settings, OBSERVABILITY_KEY: fresh_observability()}


async def test_transcription_worker_takes_one_job_at_a_time_from_its_own_queue(settings: Settings) -> None:
    worker = build_transcription_worker(settings, fresh_observability(), ArqRedis())

    assert worker.queue_name == TRANSCRIPTION_QUEUE_NAME != default_queue_name
    assert worker.max_jobs == TRANSCRIPTION_WORKER_MAX_JOBS == 1
    assert worker.job_timeout_s == settings.transcription.job_timeout_seconds
    assert set(worker.functions) == {GENERATION_TASK}
    assert worker.cron_jobs == []
    assert worker.allow_abort_jobs is True


async def test_generation_worker_still_reads_only_the_default_queue_with_the_generation_timeout(
    settings: Settings,
) -> None:
    worker = build_worker(settings, fresh_observability(), ArqRedis())

    assert worker.queue_name == default_queue_name
    assert worker.job_timeout_s == settings.limits.generation_job_timeout_seconds


def test_each_worker_type_serves_metrics_on_its_own_port(settings: Settings) -> None:
    observed = replace(
        settings,
        observability=ObservabilitySettings.model_construct(
            worker_metrics_port=9464, transcription_worker_metrics_port=9465
        ),
    )

    assert generation_metrics_port(observed) == 9464
    assert transcription_metrics_port(observed) == 9465


async def test_transcription_worker_refuses_to_start_while_transcription_is_disabled(
    settings: Settings,
) -> None:
    ctx = context(
        replace(settings, transcription=settings.transcription.model_copy(update={"enabled": False}))
    )

    with pytest.raises(RuntimeError, match="DECKLY_TRANSCRIPTION_ENABLED is false"):
        await transcription_startup(ctx)

    assert ENGINE_KEY not in ctx


async def test_transcription_worker_refuses_to_start_without_ffmpeg(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model, "which", lambda _: None)
    ctx = context(settings)

    with pytest.raises(RuntimeError, match="needs ffmpeg"):
        await transcription_startup(ctx)

    assert ENGINE_KEY not in ctx


async def test_transcription_worker_refuses_to_start_without_the_pinned_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(model, "which", lambda _: "/usr/bin/ffmpeg")
    empty = tmp_path / "no-model"
    ctx = context(
        replace(settings, transcription=settings.transcription.model_copy(update={"model_directory": empty}))
    )

    with pytest.raises(RuntimeError, match="run make model"):
        await transcription_startup(ctx)

    assert ENGINE_KEY not in ctx


def test_recording_transcriber_is_built_from_the_duration_cap_and_the_transcription_settings(
    settings: Settings,
) -> None:
    client = build_transcription_client(settings)
    transcriber = build_recording_transcriber(settings, client, Path("/usr/bin/ffmpeg"), fresh_metrics())

    assert transcriber.decoder.binary == Path("/usr/bin/ffmpeg")
    assert transcriber.decoder.max_seconds == settings.limits.recording_max_duration_seconds
    assert transcriber.decoder.deadline_seconds == settings.transcription.decode_deadline_seconds
    assert transcriber.max_characters == settings.limits.document_max_source_characters
    assert transcriber.client is client
    assert client.deadline_seconds == settings.transcription.deadline_seconds
    assert client.invocation.model_directory == settings.transcription.model_directory.resolve()
    assert client.invocation.model_directory.is_absolute()
    assert client.invocation.cpu_threads == settings.transcription.cpu_threads


@dataclass(frozen=True, slots=True)
class ClosingClient(SelfHostedTranscriptionClient):
    closed: list[bool] = field(default_factory=list)

    async def aclose(self) -> None:
        self.closed.append(True)


async def test_shutdown_closes_the_transcription_client(settings: Settings) -> None:
    built = build_transcription_client(settings)
    client = ClosingClient(invocation=built.invocation, deadline_seconds=built.deadline_seconds)

    await shutdown({TRANSCRIPTION_CLIENT_KEY: client})

    assert client.closed == [True]
