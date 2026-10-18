import os
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from deckly.config import (
    DEFAULT_DOCUMENT_UPLOAD_MAX_CONCURRENCY,
    DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES,
    DEFAULT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS,
    DEFAULT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES,
    DEFAULT_MODERATION_MAX_CONCURRENCY,
    DEFAULT_RECORDING_MAX_BYTES,
    DEFAULT_RECORDING_MAX_DURATION_SECONDS,
    DEFAULT_RECORDING_UPLOAD_MAX_CONCURRENCY,
    DEFAULT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS,
    DEFAULT_RECORDING_URL_FETCHES_PER_ADDRESS_PER_WINDOW,
    DEFAULT_RECORDING_URL_FETCHES_PER_WINDOW,
    DEFAULT_RECORDING_URL_MAX_CONCURRENCY,
    DEFAULT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW,
    DEFAULT_RECORDING_URL_TIMEOUT_SECONDS,
    DEFAULT_RECORDING_URL_WINDOW_SECONDS,
    DEFAULT_TRANSCRIPTION_CPU_THREADS,
    DEFAULT_TRANSCRIPTION_DEADLINE_SECONDS,
    DEFAULT_TRANSCRIPTION_DECODE_DEADLINE_SECONDS,
    DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS,
    DEFAULT_TRANSCRIPTION_MODEL_DIRECTORY,
    DEFAULT_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES,
    DEFAULT_TRANSCRIPTION_WORKER_METRICS_PORT,
    DOCUMENT_DOWNLOAD_PEAK_COPIES,
    DOCUMENT_PARSE_WORKING_SET_BYTES,
    DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_PARSE_DEADLINE_SECONDS,
    MAX_DOCUMENT_RETENTION_SECONDS,
    MAX_DOCUMENT_SOURCE_CHARACTERS,
    MAX_DOCUMENT_UPLOAD_CONCURRENCY,
    MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS,
    MAX_JUDGED_CANDIDATES,
    MAX_MEDIA_CANDIDATES,
    MAX_MEDIA_CONCURRENCY,
    MAX_MEDIA_IMAGES,
    MAX_MODERATION_CONCURRENCY,
    MAX_MODERATION_TIMEOUT_SECONDS,
    MAX_RECORDING_BYTES,
    MAX_RECORDING_DURATION_SECONDS,
    MAX_RECORDING_UPLOAD_CONCURRENCY,
    MAX_RECORDING_URL_CONCURRENCY,
    MAX_RECORDING_URL_TIMEOUT_SECONDS,
    MAX_STORAGE_CONCURRENCY,
    MAX_TRANSCRIPTION_CPU_THREADS,
    MAX_TRANSCRIPTION_DECODE_DEADLINE_SECONDS,
    MEASURED_TRANSCRIPTION_REAL_TIME_FACTOR,
    MEBIBYTE,
    MEDIA_URL_MIN_VALIDITY_SECONDS,
    MIN_DOCUMENT_RETENTION_SECONDS,
    MIN_JOB_RETENTION_SECONDS,
    RECORDING_URL_SUBPROCESS_PEAK_BYTES,
    TRANSCRIPTION_ENGINE_WORKING_SET_BYTES,
    TRANSCRIPTION_MEMORY_SAFETY_MARGIN,
    TRANSCRIPTION_WORKER_MAX_JOBS,
    WORKER_MAX_JOBS,
    AppSettings,
    CacheSettings,
    DatabaseSettings,
    LimitSettings,
    ObservabilitySettings,
    ProviderSettings,
    RedisSettings,
    RegenerateSettings,
    Settings,
    SweepSettings,
    TranscriptionSettings,
    load_settings,
)
from deckly.infrastructure.media.licensing import MIN_IMAGE_SIDE
from deckly.infrastructure.search.parser import MIN_VISIBLE_CHARACTERS

ASYNC_URL = "postgresql+asyncpg://deckly:deckly@localhost:5433/deckly"
QUEUE_REDIS_URL = "redis://queue.internal:6379/0"
CACHE_REDIS_URL = "redis://cache.internal:6380/2"


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for name in [name for name in os.environ if name.startswith("DECKLY_")]:
        monkeypatch.delenv(name)
    return monkeypatch


def set_database_environment(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DECKLY_DATABASE_URL", url)
    monkeypatch.setenv("DECKLY_DATABASE_POOL_SIZE", "5")
    monkeypatch.setenv("DECKLY_DATABASE_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DECKLY_DATABASE_POOL_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DECKLY_DATABASE_HEALTH_CHECK_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DECKLY_DATABASE_JOB_STORE_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("DECKLY_DATABASE_DOCUMENT_STORE_TIMEOUT_SECONDS", "5")


def test_missing_required_config_fails_fast(clean_environment: pytest.MonkeyPatch) -> None:
    del clean_environment
    with pytest.raises(ValidationError):
        load_settings()


def test_async_driver_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    set_database_environment(clean_environment, ASYNC_URL)

    assert DatabaseSettings().url.scheme == "postgresql+asyncpg"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://deckly:deckly@localhost/deckly",
        "postgresql+psycopg2://deckly:deckly@localhost/deckly",
        "postgresql+psycopg://deckly:deckly@localhost/deckly",
    ],
)
def test_sync_or_other_drivers_are_rejected(clean_environment: pytest.MonkeyPatch, url: str) -> None:
    set_database_environment(clean_environment, url)

    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        DatabaseSettings()


def test_zero_pool_size_is_rejected(clean_environment: pytest.MonkeyPatch) -> None:
    set_database_environment(clean_environment, ASYNC_URL)
    clean_environment.setenv("DECKLY_DATABASE_POOL_SIZE", "0")

    with pytest.raises(ValidationError):
        DatabaseSettings()


def test_invalid_database_url_does_not_leak_password(clean_environment: pytest.MonkeyPatch) -> None:
    set_database_environment(clean_environment, "postgresql://deckly:hunter2@localhost/deckly")

    with pytest.raises(ValidationError) as error:
        DatabaseSettings()

    assert "hunter2" not in str(error.value)


PROVIDER_ENVIRONMENT = {
    "DECKLY_PROVIDER_MODEL_PROVIDER": "anthropic",
    "DECKLY_PROVIDER_MODEL_BASE_URL": "https://api.anthropic.com",
    "DECKLY_PROVIDER_MODEL_API_KEY": "key",
    "DECKLY_PROVIDER_MODEL_NAME": "model",
    "DECKLY_PROVIDER_MODEL_MAX_OUTPUT_TOKENS": "16000",
    "DECKLY_PROVIDER_MODEL_TIMEOUT_SECONDS": "180",
    "DECKLY_PROVIDER_MODEL_DEADLINE_SECONDS": "240",
    "DECKLY_PROVIDER_MODEL_MAX_ATTEMPTS": "3",
    "DECKLY_PROVIDER_MODEL_RETRY_BASE_DELAY_SECONDS": "1",
    "DECKLY_PROVIDER_MODEL_RETRY_MAX_DELAY_SECONDS": "20",
    "DECKLY_PROVIDER_MODEL_CIRCUIT_FAILURE_THRESHOLD": "5",
    "DECKLY_PROVIDER_MODEL_CIRCUIT_RESET_SECONDS": "30",
    "DECKLY_PROVIDER_SEARCH_BASE_URL": "https://api.tavily.com",
    "DECKLY_PROVIDER_SEARCH_API_KEY": "key",
    "DECKLY_PROVIDER_SEARCH_TIMEOUT_SECONDS": "15",
    "DECKLY_PROVIDER_SEARCH_DEADLINE_SECONDS": "40",
    "DECKLY_PROVIDER_SEARCH_MAX_ATTEMPTS": "2",
    "DECKLY_PROVIDER_SEARCH_RETRY_BASE_DELAY_SECONDS": "1",
    "DECKLY_PROVIDER_SEARCH_RETRY_MAX_DELAY_SECONDS": "5",
    "DECKLY_PROVIDER_SEARCH_CIRCUIT_FAILURE_THRESHOLD": "5",
    "DECKLY_PROVIDER_SEARCH_CIRCUIT_RESET_SECONDS": "30",
    "DECKLY_PROVIDER_SEARCH_MAX_RESULTS": "6",
    "DECKLY_PROVIDER_SEARCH_MAX_SOURCE_CHARACTERS": "8000",
    "DECKLY_PROVIDER_MEDIA_BASE_URL": "https://commons.wikimedia.org",
    "DECKLY_PROVIDER_MEDIA_USER_AGENT": "Deckly/0.1 (https://example.com/contact)",
    "DECKLY_PROVIDER_MEDIA_TIMEOUT_SECONDS": "8",
    "DECKLY_PROVIDER_MEDIA_DEADLINE_SECONDS": "20",
    "DECKLY_PROVIDER_MEDIA_MAX_ATTEMPTS": "2",
    "DECKLY_PROVIDER_MEDIA_RETRY_BASE_DELAY_SECONDS": "1",
    "DECKLY_PROVIDER_MEDIA_RETRY_MAX_DELAY_SECONDS": "3",
    "DECKLY_PROVIDER_MEDIA_CIRCUIT_FAILURE_THRESHOLD": "5",
    "DECKLY_PROVIDER_MEDIA_CIRCUIT_RESET_SECONDS": "30",
    "DECKLY_PROVIDER_MEDIA_MAX_IMAGES": "20",
    "DECKLY_PROVIDER_MEDIA_CANDIDATES_PER_QUERY": "10",
    "DECKLY_PROVIDER_MEDIA_THUMBNAIL_WIDTH": "960",
    "DECKLY_PROVIDER_MEDIA_MAX_CONCURRENCY": "4",
    "DECKLY_PROVIDER_MEDIA_JUDGE_TIMEOUT_SECONDS": "10",
    "DECKLY_PROVIDER_MEDIA_JUDGE_DEADLINE_SECONDS": "15",
    "DECKLY_PROVIDER_MEDIA_JUDGE_MAX_ATTEMPTS": "2",
    "DECKLY_PROVIDER_MEDIA_JUDGED_CANDIDATES_PER_NOTE": "4",
    "DECKLY_PROVIDER_MODERATION_MODEL_NAME": "moderation-model",
    "DECKLY_PROVIDER_MODERATION_MAX_OUTPUT_TOKENS": "4000",
    "DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS": "2",
    "DECKLY_PROVIDER_MODERATION_FILTER_TIMEOUT_SECONDS": "15",
    "DECKLY_PROVIDER_MODERATION_FILTER_DEADLINE_SECONDS": "20",
    "DECKLY_PROVIDER_MODERATION_FILTER_MAX_ATTEMPTS": "2",
    "DECKLY_PROVIDER_STORAGE_ENDPOINT_URL": "https://storage.example.com",
    "DECKLY_PROVIDER_STORAGE_BUCKET": "deckly-documents",
    "DECKLY_PROVIDER_STORAGE_REGION": "auto",
    "DECKLY_PROVIDER_STORAGE_ACCESS_KEY_ID": "access-key-id",
    "DECKLY_PROVIDER_STORAGE_SECRET_ACCESS_KEY": "key",
    "DECKLY_PROVIDER_STORAGE_UPLOAD_TIMEOUT_SECONDS": "30",
    "DECKLY_PROVIDER_STORAGE_UPLOAD_DEADLINE_SECONDS": "60",
    "DECKLY_PROVIDER_STORAGE_DELETE_TIMEOUT_SECONDS": "5",
    "DECKLY_PROVIDER_STORAGE_DELETE_DEADLINE_SECONDS": "10",
    "DECKLY_PROVIDER_STORAGE_DOWNLOAD_TIMEOUT_SECONDS": "15",
    "DECKLY_PROVIDER_STORAGE_DOWNLOAD_DEADLINE_SECONDS": "15",
    "DECKLY_PROVIDER_STORAGE_MAX_ATTEMPTS": "2",
    "DECKLY_PROVIDER_STORAGE_RETRY_BASE_DELAY_SECONDS": "1",
    "DECKLY_PROVIDER_STORAGE_RETRY_MAX_DELAY_SECONDS": "5",
    "DECKLY_PROVIDER_STORAGE_CIRCUIT_FAILURE_THRESHOLD": "5",
    "DECKLY_PROVIDER_STORAGE_CIRCUIT_RESET_SECONDS": "30",
    "DECKLY_PROVIDER_STORAGE_MAX_CONCURRENCY": "8",
}
LIMIT_ENVIRONMENT = {
    "DECKLY_LIMIT_GENERATION_JOBS_PER_DAY": "20",
    "DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY": "100",
    "DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS": "300",
    "DECKLY_LIMIT_REGENERATE_NOTE_TIMEOUT_SECONDS": "10",
    "DECKLY_LIMIT_NOTE_REGENERATIONS_PER_WINDOW": "30",
    "DECKLY_LIMIT_NOTE_REGENERATION_WINDOW_SECONDS": "3600",
    "DECKLY_LIMIT_DOCUMENT_MAX_BYTES": "26214400",
    "DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_WINDOW": "10",
    "DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_ADDRESS_PER_WINDOW": "50",
    "DECKLY_LIMIT_DOCUMENT_UPLOAD_WINDOW_SECONDS": "3600",
    "DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS": "40000",
    "DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS": "1",
}
SHIPPED_DOCUMENT_MAX_BYTES = 25 * MEBIBYTE
LARGEST_DOCUMENT_JOB_PEAK_BYTES = 2 * MAX_DOCUMENT_BYTES
LARGEST_DOCUMENT_WORKER_BUDGET_BYTES = WORKER_MAX_JOBS * LARGEST_DOCUMENT_JOB_PEAK_BYTES
PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES = 80 * MEBIBYTE
LARGEST_LIMIT_THE_DEFAULT_WORKER_BUDGET_FITS = 48 * MEBIBYTE
SHIPPED_RECORDING_MAX_BYTES = 48 * MEBIBYTE
SHIPPED_RECORDING_UPLOAD_SHARE_BYTES = (
    2 * SHIPPED_RECORDING_MAX_BYTES + RECORDING_URL_SUBPROCESS_PEAK_BYTES
) * 2
SHIPPED_UPLOAD_WORST_CASE_BYTES = 1288 * MEBIBYTE
RECORDING_LIMIT_THAT_FILLS_THE_DEFAULT_UPLOAD_BUDGET = 48 * MEBIBYTE
ROOMY_JOB_TIMEOUT_SECONDS = "336"
REGENERATE_ENVIRONMENT = {
    "DECKLY_REGENERATE_RATE_LIMIT_TIMEOUT_SECONDS": "0.5",
    "DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS": "4.0",
    "DECKLY_REGENERATE_SEARCH_MAX_RESULTS": "3",
    "DECKLY_REGENERATE_SEARCH_MAX_SOURCE_CHARACTERS": "4000",
    "DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS": "3.25",
    "DECKLY_REGENERATE_MODEL_MAX_OUTPUT_TOKENS": "1000",
    "DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS": "4.0",
    "DECKLY_REGENERATE_NOTE_MODERATION_TIMEOUT_SECONDS": "1.75",
}
SWEEP_ENVIRONMENT = {
    "DECKLY_SWEEP_INTERVAL_MINUTES": "5",
    "DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS": "1200",
    "DECKLY_SWEEP_QUEUED_STALE_AFTER_SECONDS": "3600",
    "DECKLY_SWEEP_BATCH_SIZE": "500",
    "DECKLY_SWEEP_DOCUMENT_BATCH_SIZE": "100",
}
JOB_STORE_TIMEOUT_SECONDS = 5


def set_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PROVIDER_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("provider", ["anthropic", "deepseek", "gemini"])
def test_complete_provider_settings_are_accepted(
    clean_environment: pytest.MonkeyPatch, provider: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODEL_PROVIDER", provider)

    assert ProviderSettings().model_provider == provider


@pytest.mark.parametrize("variable", sorted(PROVIDER_ENVIRONMENT))
def test_every_provider_setting_is_required(clean_environment: pytest.MonkeyPatch, variable: str) -> None:
    set_provider_environment(clean_environment)
    clean_environment.delenv(variable)

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_PROVIDER_").lower()):
        ProviderSettings()


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize(
    "variable",
    ["DECKLY_PROVIDER_MODEL_API_KEY", "DECKLY_PROVIDER_MODEL_NAME", "DECKLY_PROVIDER_MEDIA_USER_AGENT"],
)
def test_blank_required_values_are_rejected(
    clean_environment: pytest.MonkeyPatch, variable: str, blank: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv(variable, blank)

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_PROVIDER_").lower()):
        ProviderSettings()


API_KEY_VARIABLES = [
    "DECKLY_PROVIDER_MODEL_API_KEY",
    "DECKLY_PROVIDER_SEARCH_API_KEY",
    "DECKLY_PROVIDER_STORAGE_SECRET_ACCESS_KEY",
]
INVISIBLE_IN_KEY: dict[str, str] = {
    "newline": "\n",
    "carriage return": "\r",
    "tab": "\t",
    "delete": "\x7f",
    "space": " ",
    "no-break space": "\u00a0",
    "zero-width space": "\u200b",
    "right-to-left override": "\u202e",
    "accented letter": "\u00e9",
    "cyrillic homoglyph": "\u0430",
}


@pytest.mark.parametrize("character", INVISIBLE_IN_KEY.values(), ids=INVISIBLE_IN_KEY.keys())
@pytest.mark.parametrize("variable", API_KEY_VARIABLES)
def test_api_key_with_a_character_outside_visible_ascii_is_rejected_without_echoing_it(
    clean_environment: pytest.MonkeyPatch, variable: str, character: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv(variable, f"test-secret-key{character}Injected")

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_PROVIDER_").lower()) as error:
        ProviderSettings()

    assert "test-secret-key" not in str(error.value)
    assert "Injected" not in str(error.value)


@pytest.mark.parametrize("variable", API_KEY_VARIABLES)
def test_api_key_with_a_nul_from_the_env_file_is_rejected_without_echoing_it(
    clean_environment: pytest.MonkeyPatch, variable: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.delenv(variable)
    Path(".env").write_text(f'{variable}="test-secret-key\x00Injected"\n', encoding="utf-8")

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_PROVIDER_").lower()) as error:
        ProviderSettings()

    assert "test-secret-key" not in str(error.value)


@pytest.mark.parametrize("variable", API_KEY_VARIABLES)
def test_api_key_spanning_the_whole_visible_ascii_range_is_accepted(
    clean_environment: pytest.MonkeyPatch, variable: str
) -> None:
    set_provider_environment(clean_environment)
    key = "".join(chr(code) for code in range(ord("!"), ord("~") + 1))
    clean_environment.setenv(variable, key)

    settings = ProviderSettings()

    assert key in {
        settings.model_api_key.get_secret_value(),
        settings.search_api_key.get_secret_value(),
        settings.storage_secret_access_key.get_secret_value(),
    }


@pytest.mark.parametrize("provider", ["openai", "Anthropic", ""])
def test_unknown_model_provider_is_rejected(clean_environment: pytest.MonkeyPatch, provider: str) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODEL_PROVIDER", provider)

    with pytest.raises(ValidationError, match="model_provider"):
        ProviderSettings()


def test_model_deadline_shorter_than_one_attempt_is_rejected(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODEL_DEADLINE_SECONDS", "179")

    with pytest.raises(ValidationError, match="deadline"):
        ProviderSettings()


def test_model_deadline_equal_to_one_attempt_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODEL_DEADLINE_SECONDS", "180")

    assert ProviderSettings().model_deadline_seconds == 180


@pytest.mark.parametrize("url", ["ftp://api.example.com", "api.example.com", "not a url"])
def test_model_base_url_must_be_an_http_url(clean_environment: pytest.MonkeyPatch, url: str) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODEL_BASE_URL", url)

    with pytest.raises(ValidationError, match="model_base_url"):
        ProviderSettings()


def test_search_deadline_shorter_than_one_attempt_is_rejected(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_SEARCH_DEADLINE_SECONDS", "14")

    with pytest.raises(ValidationError, match="search deadline"):
        ProviderSettings()


def test_search_deadline_equal_to_one_attempt_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_SEARCH_DEADLINE_SECONDS", "15")

    assert ProviderSettings().search_deadline_seconds == 15


@pytest.mark.parametrize("value", ["0", "21", "-1"])
def test_search_result_cap_outside_what_the_provider_serves_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_SEARCH_MAX_RESULTS", value)

    with pytest.raises(ValidationError, match="search_max_results"):
        ProviderSettings()


@pytest.mark.parametrize("value", ["1", "20"])
def test_search_result_cap_at_the_provider_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_SEARCH_MAX_RESULTS", value)

    assert ProviderSettings().search_max_results == int(value)


@pytest.mark.parametrize("url", ["ftp://api.tavily.com", "api.tavily.com"])
def test_search_base_url_must_be_an_http_url(clean_environment: pytest.MonkeyPatch, url: str) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_SEARCH_BASE_URL", url)

    with pytest.raises(ValidationError, match="search_base_url"):
        ProviderSettings()


def settings_from_environment(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    set_provider_environment(monkeypatch)
    environment = {
        "DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY": str(MAX_MODERATION_CONCURRENCY),
        **LIMIT_ENVIRONMENT,
        "DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS": ROOMY_JOB_TIMEOUT_SECONDS,
        **REGENERATE_ENVIRONMENT,
        **SWEEP_ENVIRONMENT,
        **overrides,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return Settings(
        app=AppSettings.model_construct(),
        database=DatabaseSettings.model_construct(job_store_timeout_seconds=JOB_STORE_TIMEOUT_SECONDS),
        redis=RedisSettings.model_construct(),
        providers=ProviderSettings(),
        limits=LimitSettings(),
        cache=CacheSettings.model_construct(),
        regenerate=RegenerateSettings(),
        sweep=SweepSettings(),
        observability=ObservabilitySettings.model_construct(),
        transcription=TranscriptionSettings(),
    )


def settings_with_job_timeout(monkeypatch: pytest.MonkeyPatch, job_timeout_seconds: int) -> Settings:
    return settings_from_environment(
        monkeypatch, DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS=str(job_timeout_seconds)
    )


@pytest.mark.parametrize("job_timeout_seconds", [335, 334, 1])
def test_provider_deadlines_that_fill_the_job_timeout_are_rejected(
    clean_environment: pytest.MonkeyPatch, job_timeout_seconds: int
) -> None:
    with pytest.raises(ValueError, match="moderation deadlines"):
        settings_with_job_timeout(clean_environment, job_timeout_seconds)


@pytest.mark.parametrize("job_timeout_seconds", [316, 325])
def test_media_deadline_that_pushes_the_total_past_the_job_timeout_is_rejected(
    clean_environment: pytest.MonkeyPatch, job_timeout_seconds: int
) -> None:
    with pytest.raises(ValueError, match="moderation deadlines"):
        settings_with_job_timeout(clean_environment, job_timeout_seconds)


def test_media_judge_deadline_that_pushes_the_total_past_the_job_timeout_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="media judge and moderation deadlines"):
        settings_from_environment(clean_environment, DECKLY_PROVIDER_MEDIA_JUDGE_DEADLINE_SECONDS="16")


def test_moderation_deadline_that_pushes_the_total_past_the_job_timeout_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="moderation deadlines"):
        settings_from_environment(clean_environment, DECKLY_PROVIDER_MODERATION_FILTER_DEADLINE_SECONDS="21")


def test_provider_deadlines_that_leave_room_in_the_job_timeout_are_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_with_job_timeout(clean_environment, 336)

    assert settings.limits.generation_job_timeout_seconds == 336


DEADLINES_SECONDS = 40 + 240 + 20 + 15 + 20
DEFAULT_GATE_WAIT_SECONDS = 2 * 3 * 20
DOCUMENT_DEADLINES_SECONDS = 15 + 1 + 20 + 240 + 20 + 15 + 20
DEFAULT_DOCUMENT_GATE_WAIT_SECONDS = 3 * 3 * 20


def settings_at_default_moderation_concurrency(
    monkeypatch: pytest.MonkeyPatch, job_timeout_seconds: int, **overrides: str
) -> Settings:
    return settings_from_environment(
        monkeypatch,
        DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY=str(DEFAULT_MODERATION_MAX_CONCURRENCY),
        DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS=str(job_timeout_seconds),
        **overrides,
    )


@pytest.mark.parametrize(
    ("concurrency", "wait_seconds"),
    [
        (1, 2 * 39 * 20),
        (4, 2 * 9 * 20),
        (DEFAULT_MODERATION_MAX_CONCURRENCY, DEFAULT_GATE_WAIT_SECONDS),
        (13, 2 * 3 * 20),
        (14, 2 * 2 * 20),
        (20, 2 * 1 * 20),
        (39, 2 * 1 * 20),
        (MAX_MODERATION_CONCURRENCY, 0),
    ],
)
def test_worst_case_moderation_gate_wait_counts_the_waves_a_last_chunk_queues_behind(
    clean_environment: pytest.MonkeyPatch, concurrency: int, wait_seconds: int
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY", str(concurrency))

    assert ProviderSettings().moderation_gate_worst_case_wait_seconds == wait_seconds


def test_worst_case_moderation_gate_wait_is_zero_when_every_chunk_of_every_job_has_a_slot(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY", str(MAX_MODERATION_CONCURRENCY))

    assert ProviderSettings().moderation_gate_worst_case_wait_seconds == 0


def test_worst_case_moderation_gate_wait_is_held_for_as_long_as_the_slower_gated_stage_may_run(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv(
        "DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY", str(DEFAULT_MODERATION_MAX_CONCURRENCY)
    )
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_JUDGE_DEADLINE_SECONDS", "30")

    assert ProviderSettings().moderation_gate_worst_case_wait_seconds == 2 * 3 * 30


@pytest.mark.parametrize("job_timeout_seconds", [DEADLINES_SECONDS + DEFAULT_GATE_WAIT_SECONDS, 400, 336])
def test_job_timeout_that_the_deadlines_fit_but_the_gate_wait_overflows_is_rejected(
    clean_environment: pytest.MonkeyPatch, job_timeout_seconds: int
) -> None:
    with pytest.raises(ValueError, match="moderation concurrency slot"):
        settings_at_default_moderation_concurrency(clean_environment, job_timeout_seconds)


def test_job_timeout_past_the_topic_path_but_not_the_document_path_with_its_extra_gated_stage_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    timeout = DEADLINES_SECONDS + DEFAULT_GATE_WAIT_SECONDS + 1

    with pytest.raises(ValueError, match=r"document-sourced job \(331s\).*3 gated stages \(180s\)"):
        settings_at_default_moderation_concurrency(clean_environment, timeout)


@pytest.mark.parametrize(
    "job_timeout_seconds",
    [DOCUMENT_DEADLINES_SECONDS + DEFAULT_DOCUMENT_GATE_WAIT_SECONDS, 500],
)
def test_job_timeout_that_the_document_deadlines_fit_but_its_gate_wait_overflows_is_rejected(
    clean_environment: pytest.MonkeyPatch, job_timeout_seconds: int
) -> None:
    with pytest.raises(ValueError, match="document-sourced job"):
        settings_at_default_moderation_concurrency(clean_environment, job_timeout_seconds)


def test_job_timeout_one_second_past_the_document_deadlines_and_three_gate_waits_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    timeout = DOCUMENT_DEADLINES_SECONDS + DEFAULT_DOCUMENT_GATE_WAIT_SECONDS + 1

    settings = settings_at_default_moderation_concurrency(clean_environment, timeout)

    assert settings.limits.generation_job_timeout_seconds == timeout
    assert settings.providers.document_moderation_gate_worst_case_wait_seconds == (
        DEFAULT_DOCUMENT_GATE_WAIT_SECONDS
    )


@pytest.mark.parametrize(
    ("overrides", "document_deadlines"),
    [
        ({"DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS": "6"}, 336),
        ({"DECKLY_PROVIDER_STORAGE_DOWNLOAD_DEADLINE_SECONDS": "20"}, 336),
        (
            {
                "DECKLY_PROVIDER_STORAGE_DOWNLOAD_DEADLINE_SECONDS": "30",
                "DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS": "30",
            },
            375,
        ),
    ],
    ids=["parse deadline", "download deadline", "shipped download and parse deadlines"],
)
def test_document_deadlines_that_fill_the_job_timeout_are_rejected_even_when_the_topic_path_fits(
    clean_environment: pytest.MonkeyPatch, overrides: dict[str, str], document_deadlines: int
) -> None:
    with pytest.raises(ValueError, match="document download, document parse and document screening"):
        settings_from_environment(
            clean_environment,
            DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS=str(document_deadlines),
            **overrides,
        )

    settings = settings_from_environment(
        clean_environment,
        DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS=str(document_deadlines + 1),
        **overrides,
    )
    assert settings.document_deadlines_seconds == document_deadlines
    assert settings.topic_deadlines_seconds == DEADLINES_SECONDS


def test_document_path_counts_the_screening_call_on_top_of_the_moderation_filter_after_generation(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(clean_environment)

    assert settings.document_deadlines_seconds == DOCUMENT_DEADLINES_SECONDS
    assert DOCUMENT_DEADLINES_SECONDS - settings.card_deadlines_seconds == 15 + 1 + 20


def test_gate_wait_error_reports_both_terms_so_the_operator_can_pick_which_to_change(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match=r"\(335s\).*\(120s\)"):
        settings_at_default_moderation_concurrency(clean_environment, 400)


def test_deadlines_that_overflow_the_job_timeout_alone_are_reported_without_blaming_the_gate(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="moderation deadlines") as raised:
        settings_at_default_moderation_concurrency(clean_environment, DEADLINES_SECONDS)

    assert "concurrency slot" not in str(raised.value)


def test_widening_the_gate_to_every_chunk_gives_the_wait_back_to_the_job_timeout(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        clean_environment,
        DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY=str(MAX_MODERATION_CONCURRENCY),
        DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS=str(DEADLINES_SECONDS + 1),
    )

    assert settings.providers.moderation_gate_worst_case_wait_seconds == 0


def test_shipped_env_example_passes_every_startup_check(
    clean_environment: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    del clean_environment
    shipped = Path(__file__).parent.parent / ".env.example"
    (tmp_path / ".env").write_text(shipped.read_text())

    settings = load_settings()

    assert settings.limits.document_upload_memory_budget_bytes == DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES
    assert settings.limits.document_worker_memory_budget_bytes == DEFAULT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES
    assert (
        settings.limits.generation_job_timeout_seconds
        > settings.topic_deadlines_seconds + settings.providers.moderation_gate_worst_case_wait_seconds
    )
    assert (
        settings.limits.generation_job_timeout_seconds
        > settings.document_deadlines_seconds
        + settings.providers.document_moderation_gate_worst_case_wait_seconds
    )


def test_source_character_limit_too_small_to_keep_any_page_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_SEARCH_MAX_SOURCE_CHARACTERS", str(MIN_VISIBLE_CHARACTERS - 1))

    with pytest.raises(ValidationError, match="search_max_source_characters"):
        ProviderSettings()


def test_source_character_limit_equal_to_the_smallest_usable_page_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_SEARCH_MAX_SOURCE_CHARACTERS", str(MIN_VISIBLE_CHARACTERS))

    assert ProviderSettings().search_max_source_characters == MIN_VISIBLE_CHARACTERS


def test_media_deadline_shorter_than_one_attempt_is_rejected(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_DEADLINE_SECONDS", "7")

    with pytest.raises(ValidationError, match="media deadline"):
        ProviderSettings()


def test_media_deadline_equal_to_one_attempt_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_DEADLINE_SECONDS", "8")

    assert ProviderSettings().media_deadline_seconds == 8


def test_media_judge_deadline_shorter_than_one_attempt_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_JUDGE_DEADLINE_SECONDS", "9")

    with pytest.raises(ValidationError, match="media judge deadline"):
        ProviderSettings()


def test_media_judge_deadline_equal_to_one_attempt_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_JUDGE_DEADLINE_SECONDS", "10")

    assert ProviderSettings().media_judge_deadline_seconds == 10


def test_moderation_filter_deadline_shorter_than_one_attempt_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_FILTER_DEADLINE_SECONDS", "14")

    with pytest.raises(ValidationError, match="moderation filter deadline"):
        ProviderSettings()


def test_moderation_filter_deadline_equal_to_one_attempt_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_FILTER_DEADLINE_SECONDS", "15")

    assert ProviderSettings().moderation_filter_deadline_seconds == 15


@pytest.mark.parametrize("value", ["0", "-1", str(MAX_MODERATION_TIMEOUT_SECONDS + 0.5), "nan", "inf"])
def test_topic_check_timeout_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS", value)

    with pytest.raises(ValidationError, match="moderation_timeout_seconds"):
        ProviderSettings()


@pytest.mark.parametrize("value", ["0.25", "1.5", str(MAX_MODERATION_TIMEOUT_SECONDS)])
def test_topic_check_timeout_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_TIMEOUT_SECONDS", value)

    assert ProviderSettings().moderation_timeout_seconds == float(value)


def test_moderation_concurrency_defaults_to_the_arq_default_of_ten_jobs_with_a_chunk_each(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)

    assert ProviderSettings().moderation_max_concurrency == DEFAULT_MODERATION_MAX_CONCURRENCY == 10


@pytest.mark.parametrize("value", [1, DEFAULT_MODERATION_MAX_CONCURRENCY, MAX_MODERATION_CONCURRENCY])
def test_moderation_concurrency_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, value: int
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY", str(value))

    assert ProviderSettings().moderation_max_concurrency == value


@pytest.mark.parametrize("value", ["0", "-1", str(MAX_MODERATION_CONCURRENCY + 1), "2.5", "many", ""])
def test_moderation_concurrency_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY", value)

    with pytest.raises(ValidationError, match="moderation_max_concurrency"):
        ProviderSettings()


@pytest.mark.parametrize("url", ["ftp://commons.wikimedia.org", "commons.wikimedia.org"])
def test_media_base_url_must_be_an_http_url(clean_environment: pytest.MonkeyPatch, url: str) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_BASE_URL", url)

    with pytest.raises(ValidationError, match="media_base_url"):
        ProviderSettings()


MEDIA_BOUNDS: dict[str, tuple[int, int]] = {
    "DECKLY_PROVIDER_MEDIA_MAX_IMAGES": (1, MAX_MEDIA_IMAGES),
    "DECKLY_PROVIDER_MEDIA_CANDIDATES_PER_QUERY": (1, MAX_MEDIA_CANDIDATES),
    "DECKLY_PROVIDER_MEDIA_MAX_CONCURRENCY": (1, MAX_MEDIA_CONCURRENCY),
    "DECKLY_PROVIDER_MEDIA_JUDGED_CANDIDATES_PER_NOTE": (1, MAX_JUDGED_CANDIDATES),
}
OUT_OF_BOUNDS = [
    (variable, value) for variable, (low, high) in MEDIA_BOUNDS.items() for value in (low - 1, high + 1)
]
AT_BOUNDS = [(variable, value) for variable, bounds in MEDIA_BOUNDS.items() for value in bounds]


@pytest.mark.parametrize(("variable", "value"), OUT_OF_BOUNDS)
def test_media_limit_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, variable: str, value: int
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv(variable, str(value))

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_PROVIDER_").lower()):
        ProviderSettings()


@pytest.mark.parametrize(("variable", "value"), AT_BOUNDS)
def test_media_limit_at_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, variable: str, value: int
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv(variable, str(value))

    settings = ProviderSettings()

    assert getattr(settings, variable.removeprefix("DECKLY_PROVIDER_").lower()) == value


def test_thumbnail_narrower_than_the_smallest_accepted_image_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_THUMBNAIL_WIDTH", str(MIN_IMAGE_SIDE - 1))

    with pytest.raises(ValidationError, match="media_thumbnail_width"):
        ProviderSettings()


def test_thumbnail_as_wide_as_the_smallest_accepted_image_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_provider_environment(clean_environment)
    clean_environment.setenv("DECKLY_PROVIDER_MEDIA_THUMBNAIL_WIDTH", str(MIN_IMAGE_SIDE))

    assert ProviderSettings().media_thumbnail_width == MIN_IMAGE_SIDE


def set_regenerate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REGENERATE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("variable", sorted(REGENERATE_ENVIRONMENT))
def test_every_regenerate_setting_is_required(clean_environment: pytest.MonkeyPatch, variable: str) -> None:
    set_regenerate_environment(clean_environment)
    clean_environment.delenv(variable)

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_REGENERATE_").lower()):
        RegenerateSettings()


@pytest.mark.parametrize(
    "variable",
    [
        "DECKLY_REGENERATE_RATE_LIMIT_TIMEOUT_SECONDS",
        "DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS",
        "DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS",
        "DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS",
        "DECKLY_REGENERATE_NOTE_MODERATION_TIMEOUT_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-0.5", "nan", "inf"])
def test_regenerate_timeouts_must_be_positive_and_finite(
    clean_environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    set_regenerate_environment(clean_environment)
    clean_environment.setenv(variable, value)

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_REGENERATE_").lower()):
        RegenerateSettings()


@pytest.mark.parametrize("value", ["0", "21"])
def test_regenerate_search_result_cap_outside_what_the_provider_serves_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    set_regenerate_environment(clean_environment)
    clean_environment.setenv("DECKLY_REGENERATE_SEARCH_MAX_RESULTS", value)

    with pytest.raises(ValidationError, match="search_max_results"):
        RegenerateSettings()


def test_regenerate_source_character_limit_too_small_to_keep_any_page_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_regenerate_environment(clean_environment)
    clean_environment.setenv(
        "DECKLY_REGENERATE_SEARCH_MAX_SOURCE_CHARACTERS", str(MIN_VISIBLE_CHARACTERS - 1)
    )

    with pytest.raises(ValidationError, match="search_max_source_characters"):
        RegenerateSettings()


@pytest.mark.parametrize(
    "variable",
    ["DECKLY_LIMIT_NOTE_REGENERATIONS_PER_WINDOW", "DECKLY_LIMIT_NOTE_REGENERATION_WINDOW_SECONDS"],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_regeneration_rate_limit_must_be_positive(
    clean_environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_LIMIT_").lower()):
        settings_from_environment(clean_environment, **{variable: value})


def test_regenerate_budget_that_exactly_fills_the_note_timeout_with_the_margin_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(clean_environment)

    assert settings.regenerate.budgeted_seconds == settings.limits.regenerate_note_timeout_seconds


def test_regenerate_budget_that_fills_the_note_timeout_only_after_float_rounding_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        clean_environment,
        DECKLY_REGENERATE_RATE_LIMIT_TIMEOUT_SECONDS="0.2",
        DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS="3.1",
        DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS="3.1",
        DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS="4.9",
        DECKLY_REGENERATE_NOTE_MODERATION_TIMEOUT_SECONDS="1.3",
    )

    assert settings.regenerate.budgeted_seconds == settings.limits.regenerate_note_timeout_seconds


def test_request_check_shares_its_slot_with_the_search_instead_of_adding_to_it(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        clean_environment,
        DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS="0.5",
        DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS="0.5",
        DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS="6.75",
    )

    assert settings.regenerate.budgeted_seconds == settings.limits.regenerate_note_timeout_seconds


@pytest.mark.parametrize(
    "overrides",
    [
        {"DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS": "3.35"},
        {"DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS": "4.1"},
        {"DECKLY_REGENERATE_RATE_LIMIT_TIMEOUT_SECONDS": "0.6"},
        {"DECKLY_REGENERATE_NOTE_MODERATION_TIMEOUT_SECONDS": "1.85"},
        {"DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS": "4.1"},
        {
            "DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS": "0.5",
            "DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS": "4.5",
        },
        {"DECKLY_LIMIT_REGENERATE_NOTE_TIMEOUT_SECONDS": "9"},
        {"DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS": "10"},
    ],
)
def test_regenerate_budget_that_eats_into_the_margin_is_rejected(
    clean_environment: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="regenerate note timeout"):
        settings_from_environment(clean_environment, **overrides)


@pytest.mark.parametrize("per_address", ["19", "1"])
def test_address_limit_below_the_client_limit_is_rejected(
    clean_environment: pytest.MonkeyPatch, per_address: str
) -> None:
    with pytest.raises(ValidationError, match="per-address generation limit"):
        settings_from_environment(
            clean_environment, DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY=per_address
        )


def test_address_limit_equal_to_the_client_limit_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    settings = settings_from_environment(
        clean_environment, DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY="20"
    )

    assert settings.limits.generation_jobs_per_address_per_day == settings.limits.generation_jobs_per_day


@pytest.mark.parametrize("value", ["0", "-1", "ten"])
def test_address_limit_must_be_a_positive_count(clean_environment: pytest.MonkeyPatch, value: str) -> None:
    with pytest.raises(ValidationError, match="generation_jobs_per_address_per_day"):
        settings_from_environment(clean_environment, DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY=value)


def test_address_limit_is_required(clean_environment: pytest.MonkeyPatch) -> None:
    set_provider_environment(clean_environment)
    for name, value in {**LIMIT_ENVIRONMENT, **REGENERATE_ENVIRONMENT}.items():
        clean_environment.setenv(name, value)
    clean_environment.delenv("DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY")

    with pytest.raises(ValidationError, match="generation_jobs_per_address_per_day"):
        LimitSettings()


def test_generation_result_ttl_is_read_from_the_environment(clean_environment: pytest.MonkeyPatch) -> None:
    clean_environment.setenv("DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS", "43200")
    clean_environment.setenv("DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_JOB_RETENTION_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS", "86400")

    assert CacheSettings().generation_result_ttl_seconds == 43200


@pytest.mark.parametrize("ttl_seconds", [1, 43200, MEDIA_URL_MIN_VALIDITY_SECONDS - 1])
def test_generation_result_ttl_below_the_media_validity_window_is_accepted(
    clean_environment: pytest.MonkeyPatch, ttl_seconds: int
) -> None:
    clean_environment.setenv("DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS", str(ttl_seconds))
    clean_environment.setenv("DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_JOB_RETENTION_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS", "86400")

    assert CacheSettings().generation_result_ttl_seconds == ttl_seconds


@pytest.mark.parametrize("ttl_seconds", ["0", "-1", str(MEDIA_URL_MIN_VALIDITY_SECONDS), "604800", "soon"])
def test_generation_result_ttl_that_could_outlive_media_urls_is_rejected(
    clean_environment: pytest.MonkeyPatch, ttl_seconds: str
) -> None:
    clean_environment.setenv("DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS", ttl_seconds)
    clean_environment.setenv("DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_JOB_RETENTION_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS", "86400")

    with pytest.raises(ValidationError, match="generation_result_ttl_seconds"):
        CacheSettings()


def set_cache_environment(monkeypatch: pytest.MonkeyPatch, *, idempotency_ttl: str, retention: str) -> None:
    monkeypatch.setenv("DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS", "43200")
    monkeypatch.setenv("DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS", idempotency_ttl)
    monkeypatch.setenv("DECKLY_CACHE_JOB_RETENTION_SECONDS", retention)
    monkeypatch.setenv("DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS", "86400")


@pytest.mark.parametrize("retention", [str(MIN_JOB_RETENTION_SECONDS - 1), "3600", "0", "-1"])
def test_job_retention_shorter_than_the_contracted_day_is_rejected(
    clean_environment: pytest.MonkeyPatch, retention: str
) -> None:
    set_cache_environment(clean_environment, idempotency_ttl="60", retention=retention)

    with pytest.raises(ValidationError, match="job_retention_seconds"):
        CacheSettings()


@pytest.mark.parametrize("retention", [MIN_JOB_RETENTION_SECONDS, 7 * MIN_JOB_RETENTION_SECONDS])
def test_job_retention_of_at_least_a_day_is_accepted(
    clean_environment: pytest.MonkeyPatch, retention: int
) -> None:
    set_cache_environment(clean_environment, idempotency_ttl="60", retention=str(retention))

    assert CacheSettings().job_retention_seconds == retention


def test_idempotency_key_outliving_its_job_is_rejected(clean_environment: pytest.MonkeyPatch) -> None:
    set_cache_environment(
        clean_environment,
        idempotency_ttl=str(MIN_JOB_RETENTION_SECONDS + 1),
        retention=str(MIN_JOB_RETENTION_SECONDS),
    )

    with pytest.raises(ValidationError, match="idempotency key ttl must not exceed the job retention"):
        CacheSettings()


@pytest.mark.parametrize("idempotency_ttl", ["1", str(MIN_JOB_RETENTION_SECONDS)])
def test_idempotency_key_expiring_no_later_than_its_job_is_accepted(
    clean_environment: pytest.MonkeyPatch, idempotency_ttl: str
) -> None:
    set_cache_environment(
        clean_environment, idempotency_ttl=idempotency_ttl, retention=str(MIN_JOB_RETENTION_SECONDS)
    )

    assert CacheSettings().idempotency_key_ttl_seconds == int(idempotency_ttl)


@pytest.mark.parametrize("idempotency_ttl", ["0", "-1"])
def test_idempotency_key_ttl_must_be_positive(
    clean_environment: pytest.MonkeyPatch, idempotency_ttl: str
) -> None:
    set_cache_environment(
        clean_environment, idempotency_ttl=idempotency_ttl, retention=str(MIN_JOB_RETENTION_SECONDS)
    )

    with pytest.raises(ValidationError, match="idempotency_key_ttl_seconds"):
        CacheSettings()


def settings_with_redis(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    monkeypatch.setenv("DECKLY_REDIS_URL", QUEUE_REDIS_URL)
    monkeypatch.setenv("DECKLY_REDIS_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DECKLY_REDIS_CONNECT_RETRIES", "3")
    set_cache_environment(monkeypatch, idempotency_ttl="86400", retention="86400")
    base = settings_from_environment(monkeypatch, **overrides)
    return replace(base, redis=RedisSettings(), cache=CacheSettings())


def test_cache_redis_url_falls_back_to_the_queue_redis_url_when_unset(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_with_redis(clean_environment)

    assert settings.cache.redis_url is None
    assert str(settings.cache_redis_url) == QUEUE_REDIS_URL
    assert settings.cache_redis_url == settings.redis.url


def test_cache_redis_url_is_used_instead_of_the_fallback_when_set(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_with_redis(clean_environment, DECKLY_CACHE_REDIS_URL=CACHE_REDIS_URL)

    assert str(settings.cache_redis_url) == CACHE_REDIS_URL
    assert str(settings.redis.url) == QUEUE_REDIS_URL


def test_cache_redis_url_is_trimmed_like_every_other_setting(clean_environment: pytest.MonkeyPatch) -> None:
    settings = settings_with_redis(clean_environment, DECKLY_CACHE_REDIS_URL=f"  {CACHE_REDIS_URL}  ")

    assert str(settings.cache_redis_url) == CACHE_REDIS_URL


@pytest.mark.parametrize("url", ["rediss://cache.internal:6380/0", "redis://:secret@cache.internal/0"])
def test_cache_redis_url_accepts_tls_and_credentials(clean_environment: pytest.MonkeyPatch, url: str) -> None:
    settings = settings_with_redis(clean_environment, DECKLY_CACHE_REDIS_URL=url)

    assert settings.cache_redis_url.scheme == url.split(":", 1)[0]
    assert settings.cache_redis_url.host == "cache.internal"


@pytest.mark.parametrize(
    "url", ["", "   ", "cache.internal:6379", "http://cache.internal:6379/0", "postgresql://cache/0"]
)
def test_cache_redis_url_that_is_blank_or_not_a_redis_url_is_rejected_not_read_as_unset(
    clean_environment: pytest.MonkeyPatch, url: str
) -> None:
    with pytest.raises(ValidationError, match="redis_url"):
        settings_with_redis(clean_environment, DECKLY_CACHE_REDIS_URL=url)


def test_invalid_cache_redis_url_does_not_leak_password(clean_environment: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as raised:
        settings_with_redis(clean_environment, DECKLY_CACHE_REDIS_URL="http://:hunter2@cache.internal/0")

    assert "hunter2" not in str(raised.value)


def set_sweep_environment(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name, value in {**SWEEP_ENVIRONMENT, **overrides}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("variable", sorted(SWEEP_ENVIRONMENT))
def test_every_sweep_setting_is_required(clean_environment: pytest.MonkeyPatch, variable: str) -> None:
    set_sweep_environment(clean_environment)
    clean_environment.delenv(variable)

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_SWEEP_").lower()):
        SweepSettings()


@pytest.mark.parametrize("interval", ["0", "-5", "61", "7", "25", "45"])
def test_sweep_interval_that_does_not_divide_the_hour_is_rejected(
    clean_environment: pytest.MonkeyPatch, interval: str
) -> None:
    set_sweep_environment(clean_environment, DECKLY_SWEEP_INTERVAL_MINUTES=interval)

    with pytest.raises(ValidationError, match="interval_minutes"):
        SweepSettings()


@pytest.mark.parametrize("interval", [1, 5, 15, 30, 60])
def test_sweep_interval_that_divides_the_hour_is_accepted(
    clean_environment: pytest.MonkeyPatch, interval: int
) -> None:
    set_sweep_environment(clean_environment, DECKLY_SWEEP_INTERVAL_MINUTES=str(interval))

    assert SweepSettings().interval_minutes == interval


@pytest.mark.parametrize(
    "variable",
    [
        "DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS",
        "DECKLY_SWEEP_QUEUED_STALE_AFTER_SECONDS",
        "DECKLY_SWEEP_BATCH_SIZE",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_sweep_thresholds_and_batch_size_must_be_positive(
    clean_environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    set_sweep_environment(clean_environment, **{variable: value})

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_SWEEP_").lower()):
        SweepSettings()


def test_queued_job_going_stale_before_a_running_one_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_sweep_environment(
        clean_environment,
        DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS="900",
        DECKLY_SWEEP_QUEUED_STALE_AFTER_SECONDS="899",
    )

    with pytest.raises(ValidationError, match="queued job must not go stale sooner than a running one"):
        SweepSettings()


def test_queued_and_running_jobs_may_go_stale_after_the_same_time(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_sweep_environment(
        clean_environment,
        DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS="900",
        DECKLY_SWEEP_QUEUED_STALE_AFTER_SECONDS="900",
    )

    assert SweepSettings().queued_stale_after_seconds == 900


@pytest.mark.parametrize(
    "stale_after", ["1", "300", str(int(ROOMY_JOB_TIMEOUT_SECONDS) + JOB_STORE_TIMEOUT_SECONDS)]
)
def test_running_job_going_stale_before_an_interrupted_job_could_record_its_failure_is_rejected(
    clean_environment: pytest.MonkeyPatch, stale_after: str
) -> None:
    with pytest.raises(ValueError, match="running job must not go stale before the generation job timeout"):
        settings_from_environment(clean_environment, DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS=stale_after)


def test_running_job_going_stale_just_after_an_interrupted_job_settles_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    stale_after = int(ROOMY_JOB_TIMEOUT_SECONDS) + JOB_STORE_TIMEOUT_SECONDS + 1

    settings = settings_from_environment(
        clean_environment,
        DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS=str(stale_after),
        DECKLY_TRANSCRIPTION_ENABLED="false",
    )

    assert settings.sweep.running_stale_after_seconds == stale_after


def test_running_job_going_stale_before_a_transcription_job_could_record_its_failure_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    stale_after = DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS + JOB_STORE_TIMEOUT_SECONDS

    with pytest.raises(ValueError, match="the longer transcription job timeout"):
        settings_from_environment(
            clean_environment, DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS=str(stale_after)
        )


def test_running_job_going_stale_just_after_a_transcription_job_settles_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    stale_after = DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS + JOB_STORE_TIMEOUT_SECONDS + 1

    settings = settings_from_environment(
        clean_environment, DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS=str(stale_after)
    )

    assert settings.longest_job_timeout_seconds == DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS


def test_shipped_staleness_outlasts_the_shipped_transcription_job_timeout() -> None:
    assert DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS + JOB_STORE_TIMEOUT_SECONDS < 1200


OBSERVABILITY_ENVIRONMENT = {
    "DECKLY_OBSERVABILITY_TRACE_EXPORTER": "console",
    "DECKLY_OBSERVABILITY_WORKER_METRICS_HOST": "127.0.0.1",
    "DECKLY_OBSERVABILITY_WORKER_METRICS_PORT": "9464",
}


def set_observability_environment(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name, value in {**OBSERVABILITY_ENVIRONMENT, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_complete_observability_settings_are_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    set_observability_environment(clean_environment)

    observability = ObservabilitySettings()

    assert observability.trace_exporter == "console"
    assert observability.worker_metrics_port == 9464


@pytest.mark.parametrize("exporter", ["otlp", "jaeger", "CONSOLE", ""])
def test_unsupported_trace_exporters_are_rejected(
    clean_environment: pytest.MonkeyPatch, exporter: str
) -> None:
    set_observability_environment(clean_environment, DECKLY_OBSERVABILITY_TRACE_EXPORTER=exporter)

    with pytest.raises(ValidationError, match="trace_exporter"):
        ObservabilitySettings()


@pytest.mark.parametrize("port", ["0", "-1", "65536", "metrics"])
def test_worker_metrics_port_must_be_a_usable_tcp_port(
    clean_environment: pytest.MonkeyPatch, port: str
) -> None:
    set_observability_environment(clean_environment, DECKLY_OBSERVABILITY_WORKER_METRICS_PORT=port)

    with pytest.raises(ValidationError, match="worker_metrics_port"):
        ObservabilitySettings()


def provider_settings_with(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> ProviderSettings:
    set_provider_environment(monkeypatch)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    return ProviderSettings()


@pytest.mark.parametrize("bucket", ["abc", "deckly-documents", "deckly.documents.v1", "a" * 63, "0bucket9"])
def test_storage_bucket_following_the_s3_naming_rules_is_accepted(
    clean_environment: pytest.MonkeyPatch, bucket: str
) -> None:
    settings = provider_settings_with(clean_environment, DECKLY_PROVIDER_STORAGE_BUCKET=bucket)

    assert settings.storage_bucket == bucket


@pytest.mark.parametrize(
    "bucket",
    ["ab", "a" * 64, "Deckly", "deckly_documents", "-deckly", "deckly-", "deckly/documents", "bücket"],
)
def test_storage_bucket_breaking_the_s3_naming_rules_is_rejected(
    clean_environment: pytest.MonkeyPatch, bucket: str
) -> None:
    with pytest.raises(ValidationError, match="storage_bucket"):
        provider_settings_with(clean_environment, DECKLY_PROVIDER_STORAGE_BUCKET=bucket)


@pytest.mark.parametrize(
    "url",
    [
        "https://0123456789abcdef.r2.cloudflarestorage.com",
        "https://s3.eu-central-1.amazonaws.com/",
        "http://localhost:9100",
    ],
)
def test_bare_storage_endpoint_of_any_s3_compatible_provider_is_accepted(
    clean_environment: pytest.MonkeyPatch, url: str
) -> None:
    settings = provider_settings_with(clean_environment, DECKLY_PROVIDER_STORAGE_ENDPOINT_URL=url)

    assert settings.storage_endpoint_url.path in {None, "/"}


@pytest.mark.parametrize(
    "url",
    [
        "https://storage.example.com/deckly-documents",
        "https://storage.example.com/?versioning",
        "https://storage.example.com/#bucket",
        "ftp://storage.example.com",
        "storage.example.com",
        "https://key:secret@storage.example.com",
    ],
)
def test_storage_endpoint_with_a_path_query_fragment_or_other_scheme_is_rejected(
    clean_environment: pytest.MonkeyPatch, url: str
) -> None:
    with pytest.raises(ValidationError, match="storage_endpoint_url"):
        provider_settings_with(clean_environment, DECKLY_PROVIDER_STORAGE_ENDPOINT_URL=url)


@pytest.mark.parametrize(
    "variable", ["DECKLY_PROVIDER_STORAGE_ACCESS_KEY_ID", "DECKLY_PROVIDER_STORAGE_REGION"]
)
@pytest.mark.parametrize("character", ["\n", " ", "\u00e9"])
def test_storage_identifier_outside_visible_ascii_is_rejected(
    clean_environment: pytest.MonkeyPatch, variable: str, character: str
) -> None:
    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_PROVIDER_").lower()):
        provider_settings_with(clean_environment, **{variable: f"auto{character}x"})


@pytest.mark.parametrize("operation", ["UPLOAD", "DELETE"])
def test_storage_deadline_shorter_than_one_attempt_is_rejected(
    clean_environment: pytest.MonkeyPatch, operation: str
) -> None:
    overrides = {
        f"DECKLY_PROVIDER_STORAGE_{operation}_TIMEOUT_SECONDS": "10",
        f"DECKLY_PROVIDER_STORAGE_{operation}_DEADLINE_SECONDS": "9",
    }

    with pytest.raises(ValidationError, match="storage deadlines must be at least"):
        provider_settings_with(clean_environment, **overrides)


@pytest.mark.parametrize("operation", ["UPLOAD", "DELETE"])
def test_storage_deadline_equal_to_one_attempt_is_accepted(
    clean_environment: pytest.MonkeyPatch, operation: str
) -> None:
    overrides = {
        f"DECKLY_PROVIDER_STORAGE_{operation}_TIMEOUT_SECONDS": "10",
        f"DECKLY_PROVIDER_STORAGE_{operation}_DEADLINE_SECONDS": "10",
    }

    assert provider_settings_with(clean_environment, **overrides).storage_max_attempts == 2


@pytest.mark.parametrize("concurrency", ["0", str(MAX_STORAGE_CONCURRENCY + 1)])
def test_storage_concurrency_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, concurrency: str
) -> None:
    with pytest.raises(ValidationError, match="storage_max_concurrency"):
        provider_settings_with(clean_environment, DECKLY_PROVIDER_STORAGE_MAX_CONCURRENCY=concurrency)


def test_storage_secret_is_never_echoed_by_the_settings() -> None:
    settings = ProviderSettings.model_construct(
        storage_secret_access_key=SecretStr("storage-credential-value")
    )

    assert "storage-credential-value" not in repr(settings)


def limit_settings_with(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> LimitSettings:
    for name, value in {**LIMIT_ENVIRONMENT, **overrides}.items():
        monkeypatch.setenv(name, value)
    return LimitSettings()


@pytest.mark.parametrize("max_bytes", [1, SHIPPED_DOCUMENT_MAX_BYTES, MAX_DOCUMENT_BYTES])
def test_document_size_limit_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, max_bytes: int
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(max_bytes),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY="1",
        DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=str(LARGEST_DOCUMENT_WORKER_BUDGET_BYTES),
    )

    assert settings.document_max_bytes == max_bytes


@pytest.mark.parametrize("max_bytes", ["0", "-1", str(MAX_DOCUMENT_BYTES + 1), "25MB"])
def test_document_size_limit_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, max_bytes: str
) -> None:
    with pytest.raises(ValidationError, match="document_max_bytes"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_MAX_BYTES=max_bytes)


def test_address_upload_limit_below_the_client_limit_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="per-address document upload limit"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_ADDRESS_PER_WINDOW="9")


def test_address_upload_limit_equal_to_the_client_limit_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(
        clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_ADDRESS_PER_WINDOW="10"
    )

    assert settings.document_uploads_per_address_per_window == settings.document_uploads_per_window


@pytest.mark.parametrize(
    "variable",
    [
        "DECKLY_LIMIT_DOCUMENT_MAX_BYTES",
        "DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_WINDOW",
        "DECKLY_LIMIT_DOCUMENT_UPLOADS_PER_ADDRESS_PER_WINDOW",
        "DECKLY_LIMIT_DOCUMENT_UPLOAD_WINDOW_SECONDS",
        "DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS",
        "DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS",
    ],
)
def test_every_document_limit_is_required(clean_environment: pytest.MonkeyPatch, variable: str) -> None:
    for name, value in LIMIT_ENVIRONMENT.items():
        clean_environment.setenv(name, value)
    clean_environment.delenv(variable)

    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_LIMIT_").lower()):
        LimitSettings()


@pytest.mark.parametrize("value", [MIN_VISIBLE_CHARACTERS, 40000, MAX_DOCUMENT_SOURCE_CHARACTERS])
def test_document_character_budget_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, value: int
) -> None:
    settings = limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS=str(value))

    assert settings.document_max_source_characters == value


@pytest.mark.parametrize("value", [str(MIN_VISIBLE_CHARACTERS - 1), str(MAX_DOCUMENT_SOURCE_CHARACTERS + 1)])
def test_document_character_budget_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError, match="document_max_source_characters"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_MAX_SOURCE_CHARACTERS=value)


@pytest.mark.parametrize("value", [1, MAX_DOCUMENT_PARSE_DEADLINE_SECONDS])
def test_document_parse_deadline_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, value: int
) -> None:
    settings = limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS=str(value))

    assert settings.document_parse_deadline_seconds == value


@pytest.mark.parametrize("value", ["0", "-1", "1.5", str(MAX_DOCUMENT_PARSE_DEADLINE_SECONDS + 1)])
def test_document_parse_deadline_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError, match="document_parse_deadline_seconds"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_PARSE_DEADLINE_SECONDS=value)


def test_document_upload_concurrency_defaults_to_four(clean_environment: pytest.MonkeyPatch) -> None:
    settings = limit_settings_with(clean_environment)

    assert settings.document_upload_max_concurrency == DEFAULT_DOCUMENT_UPLOAD_MAX_CONCURRENCY == 4


@pytest.mark.parametrize(
    "value", [1, DEFAULT_DOCUMENT_UPLOAD_MAX_CONCURRENCY, MAX_DOCUMENT_UPLOAD_CONCURRENCY]
)
def test_document_upload_concurrency_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, value: int
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY=str(value),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(
            value * SHIPPED_DOCUMENT_MAX_BYTES * DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN
            + SHIPPED_RECORDING_UPLOAD_SHARE_BYTES
        ),
    )

    assert settings.document_upload_max_concurrency == value


@pytest.mark.parametrize("value", ["0", "-1", str(MAX_DOCUMENT_UPLOAD_CONCURRENCY + 1), "2.5", "many", ""])
def test_document_upload_concurrency_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError, match="document_upload_max_concurrency"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY=value)


def test_document_upload_read_timeout_defaults_to_two_minutes(clean_environment: pytest.MonkeyPatch) -> None:
    settings = limit_settings_with(clean_environment)

    assert (
        settings.document_upload_read_timeout_seconds == DEFAULT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS == 120
    )


@pytest.mark.parametrize(
    "value", [1, DEFAULT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS, MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS]
)
def test_document_upload_read_timeout_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, value: int
) -> None:
    settings = limit_settings_with(
        clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS=str(value)
    )

    assert settings.document_upload_read_timeout_seconds == value


@pytest.mark.parametrize(
    "value", ["0", "-1", str(MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS + 1), "2.5", "slow", ""]
)
def test_document_upload_read_timeout_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError, match="document_upload_read_timeout_seconds"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS=value)


@pytest.mark.parametrize("retention", [MIN_DOCUMENT_RETENTION_SECONDS, 86400, MAX_DOCUMENT_RETENTION_SECONDS])
def test_document_retention_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, retention: int
) -> None:
    set_cache_environment(clean_environment, idempotency_ttl="86400", retention="86400")
    clean_environment.setenv("DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS", str(retention))

    assert CacheSettings().document_retention_seconds == retention


@pytest.mark.parametrize(
    "retention", ["0", str(MIN_DOCUMENT_RETENTION_SECONDS - 1), str(MAX_DOCUMENT_RETENTION_SECONDS + 1)]
)
def test_document_retention_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, retention: str
) -> None:
    set_cache_environment(clean_environment, idempotency_ttl="86400", retention="86400")
    clean_environment.setenv("DECKLY_CACHE_DOCUMENT_RETENTION_SECONDS", retention)

    with pytest.raises(ValidationError, match="document_retention_seconds"):
        CacheSettings()


def test_document_batch_that_cannot_be_deleted_within_one_sweep_interval_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="must fit within the sweep interval"):
        settings_from_environment(
            clean_environment,
            DECKLY_SWEEP_INTERVAL_MINUTES="1",
            DECKLY_SWEEP_DOCUMENT_BATCH_SIZE="41",
            DECKLY_PROVIDER_STORAGE_MAX_CONCURRENCY="8",
            DECKLY_PROVIDER_STORAGE_DELETE_DEADLINE_SECONDS="10",
        )


def test_document_batch_deleted_just_within_one_sweep_interval_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        clean_environment,
        DECKLY_SWEEP_INTERVAL_MINUTES="1",
        DECKLY_SWEEP_DOCUMENT_BATCH_SIZE="40",
        DECKLY_PROVIDER_STORAGE_MAX_CONCURRENCY="8",
        DECKLY_PROVIDER_STORAGE_DELETE_DEADLINE_SECONDS="10",
    )

    assert settings.sweep.document_batch_size == 40


def test_document_upload_memory_budget_defaults_to_the_documented_twelve_hundred_and_eighty_eight_mebibytes(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(clean_environment)

    assert settings.document_upload_memory_budget_bytes == DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES
    assert DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES == 1288 * MEBIBYTE


def test_document_upload_memory_safety_margin_is_the_measured_two_times() -> None:
    assert DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN == 2


def test_default_caps_size_limits_and_url_subprocess_exactly_fill_the_default_memory_budget(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(clean_environment)

    assert settings.upload_worst_case_bytes == (4 * 25 + 2 * 48 + 1 * 448) * MEBIBYTE * 2
    assert settings.upload_worst_case_bytes == SHIPPED_UPLOAD_WORST_CASE_BYTES
    assert settings.document_upload_memory_budget_bytes == settings.upload_worst_case_bytes


@pytest.mark.parametrize(
    ("variable", "shipped"),
    [
        ("DECKLY_LIMIT_DOCUMENT_MAX_BYTES", SHIPPED_DOCUMENT_MAX_BYTES),
        ("DECKLY_LIMIT_RECORDING_MAX_BYTES", SHIPPED_RECORDING_MAX_BYTES),
    ],
)
def test_one_byte_past_the_memory_budget_is_rejected(
    clean_environment: pytest.MonkeyPatch, variable: str, shipped: int
) -> None:
    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(SHIPPED_UPLOAD_WORST_CASE_BYTES),
            **{variable: str(shipped + 1)},
        )


def test_budget_exactly_equal_to_the_upload_worst_case_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(SHIPPED_UPLOAD_WORST_CASE_BYTES),
    )

    assert settings.upload_worst_case_bytes == settings.document_upload_memory_budget_bytes


def test_recording_limit_that_exactly_fills_the_default_upload_budget_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_RECORDING_MAX_BYTES=str(RECORDING_LIMIT_THAT_FILLS_THE_DEFAULT_UPLOAD_BUDGET),
    )

    assert settings.upload_worst_case_bytes == DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES


def test_recording_limit_one_byte_past_what_the_default_upload_budget_holds_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_RECORDING_MAX_BYTES=str(RECORDING_LIMIT_THAT_FILLS_THE_DEFAULT_UPLOAD_BUDGET + 1),
        )


def test_raising_the_recording_cap_is_paid_for_by_the_shared_upload_budget(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY="3")


def test_one_byte_under_the_memory_budget_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    settings = limit_settings_with(
        clean_environment, DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(SHIPPED_DOCUMENT_MAX_BYTES - 1)
    )

    assert settings.document_max_bytes == SHIPPED_DOCUMENT_MAX_BYTES - 1


def test_raising_the_size_limit_to_its_maximum_at_the_default_cap_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(MAX_DOCUMENT_BYTES))


def test_raising_the_cap_to_its_maximum_at_the_default_size_limit_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY=str(MAX_DOCUMENT_UPLOAD_CONCURRENCY),
        )


def test_raising_the_budget_makes_room_for_a_larger_size_limit(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(MAX_DOCUMENT_BYTES),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(
            DEFAULT_DOCUMENT_UPLOAD_MAX_CONCURRENCY
            * MAX_DOCUMENT_BYTES
            * DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN
            + SHIPPED_RECORDING_UPLOAD_SHARE_BYTES
        ),
        DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=str(LARGEST_DOCUMENT_WORKER_BUDGET_BYTES),
    )

    assert settings.document_max_bytes == MAX_DOCUMENT_BYTES


def test_memory_budget_error_reports_every_term_and_the_single_process_assumption(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError) as raised:
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(MAX_DOCUMENT_BYTES))

    message = str(raised.value)
    assert "document upload concurrency cap (4)" in message
    assert f"document size limit ({MAX_DOCUMENT_BYTES} bytes)" in message
    assert "recording upload concurrency cap (1)" in message
    assert "recording url fetch concurrency cap (1)" in message
    assert f"recording size limit ({SHIPPED_RECORDING_MAX_BYTES} bytes)" in message
    assert "safety margin (2)" in message
    assert f"measured yt-dlp and deno peak ({RECORDING_URL_SUBPROCESS_PEAK_BYTES} bytes)" in message
    worst_case = (
        4 * MAX_DOCUMENT_BYTES + 2 * SHIPPED_RECORDING_MAX_BYTES + RECORDING_URL_SUBPROCESS_PEAK_BYTES
    ) * 2
    assert f"is {worst_case} bytes" in message
    assert "DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY" in message
    assert "DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY" in message
    assert "DECKLY_LIMIT_RECORDING_MAX_BYTES" in message
    assert f"budget ({DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES} bytes)" in message
    assert "DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES" in message
    assert "assumes a single process" in message


@pytest.mark.parametrize("value", ["0", "-1", "400MiB", ""])
def test_document_upload_memory_budget_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError, match="document_upload_memory_budget_bytes"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=value)


def test_document_worker_memory_budget_defaults_to_the_documented_twelve_hundred_and_eighty_mebibytes(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(clean_environment)

    assert settings.document_worker_memory_budget_bytes == DEFAULT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES
    assert DEFAULT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES == 1280 * MEBIBYTE


def test_document_worker_memory_model_uses_the_measured_figures() -> None:
    assert DOCUMENT_DOWNLOAD_PEAK_COPIES == 2
    assert DOCUMENT_PARSE_WORKING_SET_BYTES == PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES


@pytest.mark.parametrize(
    ("max_bytes", "peak_bytes"),
    [
        (1, 1 + PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES),
        (SHIPPED_DOCUMENT_MAX_BYTES, SHIPPED_DOCUMENT_MAX_BYTES + PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES),
        (
            PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES - 1,
            PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES - 1 + PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES,
        ),
        (PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES, 2 * PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES),
        (PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES + 1, 2 * (PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES + 1)),
        (MAX_DOCUMENT_BYTES, 2 * MAX_DOCUMENT_BYTES),
    ],
)
def test_document_job_peak_is_the_larger_of_the_download_and_parse_phases(
    clean_environment: pytest.MonkeyPatch, max_bytes: int, peak_bytes: int
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(max_bytes),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY="1",
        DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=str(LARGEST_DOCUMENT_WORKER_BUDGET_BYTES),
    )

    assert settings.document_worker_job_peak_bytes == peak_bytes


def test_default_size_limit_fits_the_default_worker_memory_budget_and_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(clean_environment)

    assert (
        WORKER_MAX_JOBS * settings.document_worker_job_peak_bytes
        < settings.document_worker_memory_budget_bytes
    )


def test_budget_exactly_equal_to_the_worst_case_is_accepted(clean_environment: pytest.MonkeyPatch) -> None:
    worst_case = WORKER_MAX_JOBS * (SHIPPED_DOCUMENT_MAX_BYTES + PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES)

    settings = limit_settings_with(
        clean_environment, DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=str(worst_case)
    )

    assert settings.document_worker_memory_budget_bytes == worst_case


def test_budget_one_byte_under_the_worst_case_is_rejected(clean_environment: pytest.MonkeyPatch) -> None:
    worst_case = WORKER_MAX_JOBS * (SHIPPED_DOCUMENT_MAX_BYTES + PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES)

    with pytest.raises(ValidationError, match="exceeds the worker memory budget"):
        limit_settings_with(
            clean_environment, DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=str(worst_case - 1)
        )


def test_largest_size_limit_the_default_worker_budget_holds_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(LARGEST_LIMIT_THE_DEFAULT_WORKER_BUDGET_FITS),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY="1",
    )

    assert settings.document_max_bytes == LARGEST_LIMIT_THE_DEFAULT_WORKER_BUDGET_FITS


def test_one_byte_past_the_largest_size_limit_the_default_worker_budget_holds_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="exceeds the worker memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(LARGEST_LIMIT_THE_DEFAULT_WORKER_BUDGET_FITS + 1),
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY="1",
        )


def test_maximum_size_limit_at_the_default_worker_budget_is_rejected_even_with_one_upload_slot(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="exceeds the worker memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(MAX_DOCUMENT_BYTES),
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY="1",
        )


def test_worker_memory_budget_error_reports_every_term_and_the_single_process_assumption(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError) as raised:
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(MAX_DOCUMENT_BYTES),
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY="1",
        )

    message = str(raised.value)
    assert f"worker job concurrency ({WORKER_MAX_JOBS})" in message
    assert f"({2 * MAX_DOCUMENT_BYTES} bytes" in message
    assert f"size limit ({MAX_DOCUMENT_BYTES} bytes)" in message
    assert f"{DOCUMENT_PARSE_WORKING_SET_BYTES} bytes of parser working set" in message
    assert f"is {LARGEST_DOCUMENT_WORKER_BUDGET_BYTES} bytes" in message
    assert f"worker memory budget ({DEFAULT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES} bytes)" in message
    assert "DECKLY_LIMIT_DOCUMENT_MAX_BYTES" in message
    assert "DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES" in message
    assert "assumes a single worker process" in message


def test_recording_size_limit_no_longer_moves_the_generation_worker_check(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_DOCUMENT_MAX_BYTES="1",
        DECKLY_LIMIT_RECORDING_MAX_BYTES=str(MAX_RECORDING_BYTES),
        DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY="1",
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(
            (4 + 2 * MAX_RECORDING_BYTES + RECORDING_URL_SUBPROCESS_PEAK_BYTES) * 2
        ),
    )

    assert settings.document_worker_job_peak_bytes == 1 + PARSE_AND_DOWNLOAD_PEAKS_MEET_BYTES


def test_worker_budget_does_not_relax_the_upload_check(clean_environment: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(MAX_DOCUMENT_BYTES),
            DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=str(LARGEST_DOCUMENT_WORKER_BUDGET_BYTES),
        )


def test_upload_budget_does_not_relax_the_worker_check(clean_environment: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="exceeds the worker memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_DOCUMENT_MAX_BYTES=str(MAX_DOCUMENT_BYTES),
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY="1",
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(LARGEST_DOCUMENT_WORKER_BUDGET_BYTES),
        )


@pytest.mark.parametrize("value", ["0", "-1", "1GiB", "1.5", ""])
def test_document_worker_memory_budget_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ValidationError, match="document_worker_memory_budget_bytes"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=value)


def test_recording_limits_default_to_the_documented_values(clean_environment: pytest.MonkeyPatch) -> None:
    settings = limit_settings_with(clean_environment)

    assert settings.recording_max_bytes == DEFAULT_RECORDING_MAX_BYTES == SHIPPED_RECORDING_MAX_BYTES
    assert settings.recording_max_duration_seconds == DEFAULT_RECORDING_MAX_DURATION_SECONDS == 15 * 60
    assert settings.recording_upload_max_concurrency == DEFAULT_RECORDING_UPLOAD_MAX_CONCURRENCY == 1
    assert (
        settings.recording_upload_read_timeout_seconds == DEFAULT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS == 240
    )


def test_recording_duration_cap_can_be_raised_to_four_hours_for_a_paid_transcription_api() -> None:
    assert MAX_RECORDING_DURATION_SECONDS == 4 * 60 * 60


@pytest.mark.parametrize(
    ("variable", "field", "value"),
    [
        ("DECKLY_LIMIT_RECORDING_MAX_BYTES", "recording_max_bytes", 1),
        ("DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS", "recording_max_duration_seconds", 1),
        (
            "DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS",
            "recording_max_duration_seconds",
            MAX_RECORDING_DURATION_SECONDS,
        ),
        ("DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY", "recording_upload_max_concurrency", 1),
        ("DECKLY_LIMIT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS", "recording_upload_read_timeout_seconds", 1),
        (
            "DECKLY_LIMIT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS",
            "recording_upload_read_timeout_seconds",
            MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS,
        ),
    ],
)
def test_recording_limit_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, variable: str, field: str, value: int
) -> None:
    settings = limit_settings_with(clean_environment, **{variable: str(value)})

    assert getattr(settings, field) == value


def test_largest_recording_size_limit_and_concurrency_are_accepted_with_a_budget_that_holds_them(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_RECORDING_MAX_BYTES=str(MAX_RECORDING_BYTES),
        DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY=str(MAX_RECORDING_UPLOAD_CONCURRENCY),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(
            (
                4 * SHIPPED_DOCUMENT_MAX_BYTES
                + (MAX_RECORDING_UPLOAD_CONCURRENCY + 1) * MAX_RECORDING_BYTES
                + RECORDING_URL_SUBPROCESS_PEAK_BYTES
            )
            * 2
        ),
        DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES=str(LARGEST_DOCUMENT_WORKER_BUDGET_BYTES),
    )

    assert settings.recording_max_bytes == MAX_RECORDING_BYTES
    assert settings.recording_upload_max_concurrency == MAX_RECORDING_UPLOAD_CONCURRENCY


@pytest.mark.parametrize(
    ("variable", "field", "value"),
    [
        ("DECKLY_LIMIT_RECORDING_MAX_BYTES", "recording_max_bytes", "0"),
        ("DECKLY_LIMIT_RECORDING_MAX_BYTES", "recording_max_bytes", str(MAX_RECORDING_BYTES + 1)),
        ("DECKLY_LIMIT_RECORDING_MAX_BYTES", "recording_max_bytes", "48MiB"),
        ("DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS", "recording_max_duration_seconds", "0"),
        (
            "DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS",
            "recording_max_duration_seconds",
            str(MAX_RECORDING_DURATION_SECONDS + 1),
        ),
        ("DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS", "recording_max_duration_seconds", "15.5"),
        ("DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY", "recording_upload_max_concurrency", "0"),
        (
            "DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY",
            "recording_upload_max_concurrency",
            str(MAX_RECORDING_UPLOAD_CONCURRENCY + 1),
        ),
        ("DECKLY_LIMIT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS", "recording_upload_read_timeout_seconds", "0"),
        (
            "DECKLY_LIMIT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS",
            "recording_upload_read_timeout_seconds",
            str(MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS + 1),
        ),
    ],
)
def test_recording_limit_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, variable: str, field: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        limit_settings_with(clean_environment, **{variable: value})


def test_recording_url_limits_default_to_the_documented_values(clean_environment: pytest.MonkeyPatch) -> None:
    settings = limit_settings_with(clean_environment)

    assert settings.recording_url_max_concurrency == DEFAULT_RECORDING_URL_MAX_CONCURRENCY == 1
    assert settings.recording_url_timeout_seconds == DEFAULT_RECORDING_URL_TIMEOUT_SECONDS == 120
    assert settings.recording_url_fetches_per_window == DEFAULT_RECORDING_URL_FETCHES_PER_WINDOW == 5
    assert (
        settings.recording_url_fetches_per_address_per_window
        == DEFAULT_RECORDING_URL_FETCHES_PER_ADDRESS_PER_WINDOW
        == 20
    )
    assert (
        settings.recording_url_server_fetches_per_window
        == DEFAULT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW
        == 200
    )
    assert settings.recording_url_window_seconds == DEFAULT_RECORDING_URL_WINDOW_SECONDS == 3600


def test_default_upload_and_url_slots_fit_the_default_memory_budget_with_the_documented_margin(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = limit_settings_with(clean_environment)

    assert settings.upload_worst_case_bytes == (4 * 25 + 1 * 48 + 1 * 48 + 1 * 448) * 2 * MEBIBYTE
    assert settings.upload_worst_case_bytes == DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES


def test_a_second_recording_url_slot_does_not_fit_the_default_memory_budget(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="recording url fetch concurrency cap \\(2\\)"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY="2")


@pytest.mark.parametrize(
    ("variable", "field", "value"),
    [
        ("DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS", "recording_url_timeout_seconds", 1),
        (
            "DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS",
            "recording_url_timeout_seconds",
            MAX_RECORDING_URL_TIMEOUT_SECONDS,
        ),
        ("DECKLY_LIMIT_RECORDING_URL_WINDOW_SECONDS", "recording_url_window_seconds", 1),
        (
            "DECKLY_LIMIT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW",
            "recording_url_server_fetches_per_window",
            20,
        ),
    ],
)
def test_recording_url_limit_within_its_bounds_is_accepted(
    clean_environment: pytest.MonkeyPatch, variable: str, field: str, value: int
) -> None:
    settings = limit_settings_with(clean_environment, **{variable: str(value)})

    assert getattr(settings, field) == value


def test_largest_recording_url_concurrency_is_accepted_with_a_budget_that_holds_it(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    held = (
        4 * SHIPPED_DOCUMENT_MAX_BYTES
        + (1 + MAX_RECORDING_URL_CONCURRENCY) * SHIPPED_RECORDING_MAX_BYTES
        + MAX_RECORDING_URL_CONCURRENCY * RECORDING_URL_SUBPROCESS_PEAK_BYTES
    )
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY=str(MAX_RECORDING_URL_CONCURRENCY),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(held * 2),
    )

    assert settings.recording_url_max_concurrency == MAX_RECORDING_URL_CONCURRENCY


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY", "0"),
        ("DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY", str(MAX_RECORDING_URL_CONCURRENCY + 1)),
        ("DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS", "0"),
        ("DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS", str(MAX_RECORDING_URL_TIMEOUT_SECONDS + 1)),
        ("DECKLY_LIMIT_RECORDING_URL_TIMEOUT_SECONDS", "1.5"),
        ("DECKLY_LIMIT_RECORDING_URL_FETCHES_PER_WINDOW", "0"),
        ("DECKLY_LIMIT_RECORDING_URL_FETCHES_PER_ADDRESS_PER_WINDOW", "-1"),
        ("DECKLY_LIMIT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW", "0"),
        ("DECKLY_LIMIT_RECORDING_URL_WINDOW_SECONDS", "0"),
        ("DECKLY_LIMIT_RECORDING_URL_WINDOW_SECONDS", ""),
    ],
)
def test_recording_url_limit_outside_its_bounds_is_rejected(
    clean_environment: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=variable.removeprefix("DECKLY_LIMIT_").lower()):
        limit_settings_with(clean_environment, **{variable: value})


def test_per_address_recording_url_limit_below_the_per_client_one_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="per-address recording url fetch limit"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_RECORDING_URL_FETCHES_PER_WINDOW="6",
            DECKLY_LIMIT_RECORDING_URL_FETCHES_PER_ADDRESS_PER_WINDOW="5",
        )


def test_server_recording_url_limit_below_the_per_address_one_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="server-wide recording url fetch limit"):
        limit_settings_with(clean_environment, DECKLY_LIMIT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW="19")


def test_recording_url_subprocess_peak_is_the_measured_figure_rounded_up() -> None:
    assert RECORDING_URL_SUBPROCESS_PEAK_BYTES == 448 * MEBIBYTE


def url_slots_worst_case(url_slots: int) -> int:
    held = (
        4 * SHIPPED_DOCUMENT_MAX_BYTES
        + (1 + url_slots) * SHIPPED_RECORDING_MAX_BYTES
        + url_slots * RECORDING_URL_SUBPROCESS_PEAK_BYTES
    )
    return held * DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN


@pytest.mark.parametrize("url_slots", [1, 2, MAX_RECORDING_URL_CONCURRENCY])
def test_budget_exactly_holding_the_url_slots_and_their_subprocesses_is_accepted(
    clean_environment: pytest.MonkeyPatch, url_slots: int
) -> None:
    settings = limit_settings_with(
        clean_environment,
        DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY=str(url_slots),
        DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(url_slots_worst_case(url_slots)),
    )

    assert settings.upload_worst_case_bytes == settings.document_upload_memory_budget_bytes


@pytest.mark.parametrize("url_slots", [1, 2, MAX_RECORDING_URL_CONCURRENCY])
def test_one_byte_short_of_the_url_slots_and_their_subprocesses_is_rejected(
    clean_environment: pytest.MonkeyPatch, url_slots: int
) -> None:
    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(
            clean_environment,
            DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY=str(url_slots),
            DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(url_slots_worst_case(url_slots) - 1),
        )


def test_subprocess_term_takes_the_same_safety_margin_as_every_other_term(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    undoubled = SHIPPED_UPLOAD_WORST_CASE_BYTES - RECORDING_URL_SUBPROCESS_PEAK_BYTES

    with pytest.raises(ValidationError, match="exceeds the upload memory budget"):
        limit_settings_with(
            clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(undoubled)
        )


def test_the_previous_four_hundred_mebibyte_budget_no_longer_holds_one_url_fetch(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="measured yt-dlp and deno peak"):
        limit_settings_with(
            clean_environment, DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES=str(400 * MEBIBYTE)
        )


RECORDING_DEADLINES_SECONDS = (
    15 + DEFAULT_TRANSCRIPTION_DECODE_DEADLINE_SECONDS + 540 + 20 + 240 + 20 + 15 + 20
)
SHIPPED_DECODED_AUDIO_BYTES = 900 * 16_000 * 2
SHIPPED_TRANSCRIPTION_WORST_CASE_BYTES = (
    SHIPPED_RECORDING_MAX_BYTES + TRANSCRIPTION_ENGINE_WORKING_SET_BYTES + SHIPPED_DECODED_AUDIO_BYTES
) * 2


def test_transcription_settings_default_to_the_documented_values(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = TranscriptionSettings()

    assert settings.enabled is True
    assert (
        settings.model_directory
        == DEFAULT_TRANSCRIPTION_MODEL_DIRECTORY
        == Path("models/faster-whisper-small")
    )
    assert settings.cpu_threads == DEFAULT_TRANSCRIPTION_CPU_THREADS == 2
    assert settings.deadline_seconds == DEFAULT_TRANSCRIPTION_DEADLINE_SECONDS == 540
    assert settings.decode_deadline_seconds == DEFAULT_TRANSCRIPTION_DECODE_DEADLINE_SECONDS == 30
    assert settings.job_timeout_seconds == DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS == 960
    assert settings.worker_memory_budget_bytes == DEFAULT_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES
    assert DEFAULT_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES == 2560 * MEBIBYTE
    assert clean_environment is not None


def test_transcription_budget_model_uses_the_measured_figures_and_the_shared_margin() -> None:
    assert MEASURED_TRANSCRIPTION_REAL_TIME_FACTOR == 0.14
    assert TRANSCRIPTION_ENGINE_WORKING_SET_BYTES == 1024 * MEBIBYTE
    assert TRANSCRIPTION_MEMORY_SAFETY_MARGIN == DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN == 2
    assert TRANSCRIPTION_WORKER_MAX_JOBS == 1


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DECKLY_TRANSCRIPTION_CPU_THREADS", "0"),
        ("DECKLY_TRANSCRIPTION_CPU_THREADS", str(MAX_TRANSCRIPTION_CPU_THREADS + 1)),
        ("DECKLY_TRANSCRIPTION_DEADLINE_SECONDS", "0"),
        ("DECKLY_TRANSCRIPTION_DEADLINE_SECONDS", str(MAX_RECORDING_DURATION_SECONDS + 1)),
        ("DECKLY_TRANSCRIPTION_DECODE_DEADLINE_SECONDS", "0"),
        ("DECKLY_TRANSCRIPTION_DECODE_DEADLINE_SECONDS", str(MAX_TRANSCRIPTION_DECODE_DEADLINE_SECONDS + 1)),
        ("DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS", "0"),
        ("DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES", "-1"),
        ("DECKLY_TRANSCRIPTION_ENABLED", "sometimes"),
        ("DECKLY_TRANSCRIPTION_MODEL_DIRECTORY", ""),
    ],
)
def test_transcription_settings_outside_their_bounds_are_rejected(
    clean_environment: pytest.MonkeyPatch, name: str, value: str
) -> None:
    clean_environment.setenv(name, value)

    with pytest.raises(ValidationError, match=name.removeprefix("DECKLY_TRANSCRIPTION_").lower()):
        TranscriptionSettings()


def test_recording_path_adds_decode_and_transcription_to_the_document_deadlines(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(clean_environment)

    assert settings.recording_deadlines_seconds == RECORDING_DEADLINES_SECONDS
    assert settings.providers.recording_moderation_gate_worst_case_wait_seconds == 0


def test_transcription_job_timeout_one_second_past_the_recording_deadlines_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        clean_environment, DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS=str(RECORDING_DEADLINES_SECONDS + 1)
    )

    assert settings.transcription.job_timeout_seconds == RECORDING_DEADLINES_SECONDS + 1


def test_transcription_job_timeout_the_recording_deadlines_fill_is_rejected_with_every_term_named(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="recording-sourced job") as raised:
        settings_from_environment(
            clean_environment, DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS=str(RECORDING_DEADLINES_SECONDS)
        )

    message = str(raised.value)
    assert (
        f"({RECORDING_DEADLINES_SECONDS}s: the document download, the audio decode, the transcription"
        in message
    )
    assert "DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS" in message


SINGLE_MODERATION_SLOT = {
    "DECKLY_PROVIDER_MODERATION_MAX_CONCURRENCY": "1",
    "DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS": "3000",
    "DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS": "3100",
}


def test_moderation_gate_wait_of_the_one_job_transcription_worker_counts_against_its_timeout(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    gate_wait = 3 * 3 * 20
    budget = RECORDING_DEADLINES_SECONDS + gate_wait

    with pytest.raises(ValueError, match=f"gated stages \\({gate_wait}s\\)"):
        settings_from_environment(
            clean_environment,
            **SINGLE_MODERATION_SLOT,
            DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS=str(budget),
        )
    settings = settings_from_environment(
        clean_environment,
        **SINGLE_MODERATION_SLOT,
        DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS=str(budget + 1),
    )
    assert settings.providers.recording_moderation_gate_worst_case_wait_seconds == gate_wait


def test_transcription_deadline_at_the_measured_floor_for_the_duration_cap_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    floor = 252

    settings = settings_from_environment(clean_environment, DECKLY_TRANSCRIPTION_DEADLINE_SECONDS=str(floor))

    assert settings.transcription_deadline_floor_seconds == floor


def test_transcription_deadline_under_the_measured_floor_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="DECKLY_TRANSCRIPTION_DEADLINE_SECONDS \\(251s\\)"):
        settings_from_environment(clean_environment, DECKLY_TRANSCRIPTION_DEADLINE_SECONDS="251")


def test_raising_the_duration_cap_without_the_deadline_is_rejected(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        ValueError, match="raise the deadline or lower DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS"
    ):
        settings_from_environment(clean_environment, DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS="3600")


def test_shipped_limits_fit_the_shipped_transcription_worker_budget(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(clean_environment)

    assert settings.transcription_worker_worst_case_bytes == SHIPPED_TRANSCRIPTION_WORST_CASE_BYTES
    assert SHIPPED_TRANSCRIPTION_WORST_CASE_BYTES < DEFAULT_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES


def test_transcription_budget_exactly_equal_to_the_worst_case_is_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        clean_environment,
        DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES=str(SHIPPED_TRANSCRIPTION_WORST_CASE_BYTES),
    )

    assert settings.transcription.worker_memory_budget_bytes == SHIPPED_TRANSCRIPTION_WORST_CASE_BYTES


def test_transcription_budget_one_byte_under_the_worst_case_is_rejected_with_every_term_named(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="exceeds the transcription worker memory budget") as raised:
        settings_from_environment(
            clean_environment,
            DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES=str(SHIPPED_TRANSCRIPTION_WORST_CASE_BYTES - 1),
        )

    message = str(raised.value)
    assert f"job concurrency ({TRANSCRIPTION_WORKER_MAX_JOBS})" in message
    assert f"recording size limit, {SHIPPED_RECORDING_MAX_BYTES} bytes" in message
    assert f"measured {TRANSCRIPTION_ENGINE_WORKING_SET_BYTES} bytes of working set" in message
    assert "900s at 32000 bytes a second" in message
    assert f"is {SHIPPED_TRANSCRIPTION_WORST_CASE_BYTES} bytes" in message
    assert "DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES" in message
    assert "assumes a single transcription worker process" in message


def test_longer_duration_cap_grows_the_decoded_audio_term_until_the_budget_refuses_it(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    hour = {
        "DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS": "3600",
        "DECKLY_TRANSCRIPTION_DEADLINE_SECONDS": "1100",
        "DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS": "1600",
        "DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS": "1700",
    }
    settings = settings_from_environment(clean_environment, **hour)
    assert settings.transcription_engine_peak_bytes == TRANSCRIPTION_ENGINE_WORKING_SET_BYTES + 3600 * 32_000

    with pytest.raises(ValueError, match="exceeds the transcription worker memory budget"):
        settings_from_environment(
            clean_environment,
            **{
                **hour,
                "DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS": str(MAX_RECORDING_DURATION_SECONDS),
                "DECKLY_TRANSCRIPTION_DEADLINE_SECONDS": str(MAX_RECORDING_DURATION_SECONDS),
                "DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS": "20000",
                "DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS": "20100",
                "DECKLY_SWEEP_QUEUED_STALE_AFTER_SECONDS": "20100",
            },
        )


def test_disabled_transcription_skips_every_transcription_check(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_from_environment(
        clean_environment,
        DECKLY_TRANSCRIPTION_ENABLED="false",
        DECKLY_TRANSCRIPTION_DEADLINE_SECONDS="1",
        DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS="1",
        DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES="1",
        DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS=str(
            int(ROOMY_JOB_TIMEOUT_SECONDS) + JOB_STORE_TIMEOUT_SECONDS + 1
        ),
    )

    assert settings.longest_job_timeout_seconds == int(ROOMY_JOB_TIMEOUT_SECONDS)


def test_transcription_worker_serves_metrics_on_its_own_port_by_default(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    set_observability_environment(clean_environment)

    observability = ObservabilitySettings()

    assert (
        observability.transcription_worker_metrics_port == DEFAULT_TRANSCRIPTION_WORKER_METRICS_PORT == 9465
    )


def test_both_workers_on_one_metrics_port_are_rejected(clean_environment: pytest.MonkeyPatch) -> None:
    set_observability_environment(
        clean_environment, DECKLY_OBSERVABILITY_TRANSCRIPTION_WORKER_METRICS_PORT="9464"
    )

    with pytest.raises(ValidationError, match="must serve metrics on different ports"):
        ObservabilitySettings()
