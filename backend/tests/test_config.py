import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from deckly.config import (
    MAX_JUDGED_CANDIDATES,
    MAX_MEDIA_CANDIDATES,
    MAX_MEDIA_CONCURRENCY,
    MAX_MEDIA_IMAGES,
    MAX_MODERATION_TIMEOUT_SECONDS,
    MEDIA_URL_MIN_VALIDITY_SECONDS,
    MIN_JOB_RETENTION_SECONDS,
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
    load_settings,
)
from deckly.infrastructure.media.licensing import MIN_IMAGE_SIDE
from deckly.infrastructure.search.parser import MIN_VISIBLE_CHARACTERS

ASYNC_URL = "postgresql+asyncpg://deckly:deckly@localhost:5433/deckly"


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
}
LIMIT_ENVIRONMENT = {
    "DECKLY_LIMIT_GENERATION_JOBS_PER_DAY": "20",
    "DECKLY_LIMIT_GENERATION_JOBS_PER_ADDRESS_PER_DAY": "100",
    "DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS": "300",
    "DECKLY_LIMIT_REGENERATE_NOTE_TIMEOUT_SECONDS": "10",
    "DECKLY_LIMIT_NOTE_REGENERATIONS_PER_WINDOW": "30",
    "DECKLY_LIMIT_NOTE_REGENERATION_WINDOW_SECONDS": "3600",
}
ROOMY_JOB_TIMEOUT_SECONDS = "336"
REGENERATE_ENVIRONMENT = {
    "DECKLY_REGENERATE_RATE_LIMIT_TIMEOUT_SECONDS": "0.5",
    "DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS": "2.5",
    "DECKLY_REGENERATE_SEARCH_MAX_RESULTS": "3",
    "DECKLY_REGENERATE_SEARCH_MAX_SOURCE_CHARACTERS": "4000",
    "DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS": "4.5",
    "DECKLY_REGENERATE_MODEL_MAX_OUTPUT_TOKENS": "1000",
    "DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS": "2.5",
    "DECKLY_REGENERATE_NOTE_MODERATION_TIMEOUT_SECONDS": "1.5",
}
SWEEP_ENVIRONMENT = {
    "DECKLY_SWEEP_INTERVAL_MINUTES": "5",
    "DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS": "900",
    "DECKLY_SWEEP_QUEUED_STALE_AFTER_SECONDS": "3600",
    "DECKLY_SWEEP_BATCH_SIZE": "500",
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


API_KEY_VARIABLES = ["DECKLY_PROVIDER_MODEL_API_KEY", "DECKLY_PROVIDER_SEARCH_API_KEY"]
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

    assert key in {settings.model_api_key.get_secret_value(), settings.search_api_key.get_secret_value()}


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
        DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS="2.6",
        DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS="2.6",
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
        DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS="6.5",
    )

    assert settings.regenerate.budgeted_seconds == settings.limits.regenerate_note_timeout_seconds


@pytest.mark.parametrize(
    "overrides",
    [
        {"DECKLY_REGENERATE_MODEL_TIMEOUT_SECONDS": "4.6"},
        {"DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS": "2.6"},
        {"DECKLY_REGENERATE_RATE_LIMIT_TIMEOUT_SECONDS": "0.6"},
        {"DECKLY_REGENERATE_NOTE_MODERATION_TIMEOUT_SECONDS": "1.6"},
        {"DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS": "2.6"},
        {
            "DECKLY_REGENERATE_SEARCH_TIMEOUT_SECONDS": "0.5",
            "DECKLY_REGENERATE_REQUEST_MODERATION_TIMEOUT_SECONDS": "3.5",
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

    assert CacheSettings().generation_result_ttl_seconds == 43200


@pytest.mark.parametrize("ttl_seconds", [1, 43200, MEDIA_URL_MIN_VALIDITY_SECONDS - 1])
def test_generation_result_ttl_below_the_media_validity_window_is_accepted(
    clean_environment: pytest.MonkeyPatch, ttl_seconds: int
) -> None:
    clean_environment.setenv("DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS", str(ttl_seconds))
    clean_environment.setenv("DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_JOB_RETENTION_SECONDS", "86400")

    assert CacheSettings().generation_result_ttl_seconds == ttl_seconds


@pytest.mark.parametrize("ttl_seconds", ["0", "-1", str(MEDIA_URL_MIN_VALIDITY_SECONDS), "604800", "soon"])
def test_generation_result_ttl_that_could_outlive_media_urls_is_rejected(
    clean_environment: pytest.MonkeyPatch, ttl_seconds: str
) -> None:
    clean_environment.setenv("DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS", ttl_seconds)
    clean_environment.setenv("DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS", "86400")
    clean_environment.setenv("DECKLY_CACHE_JOB_RETENTION_SECONDS", "86400")

    with pytest.raises(ValidationError, match="generation_result_ttl_seconds"):
        CacheSettings()


def set_cache_environment(monkeypatch: pytest.MonkeyPatch, *, idempotency_ttl: str, retention: str) -> None:
    monkeypatch.setenv("DECKLY_CACHE_GENERATION_RESULT_TTL_SECONDS", "43200")
    monkeypatch.setenv("DECKLY_CACHE_IDEMPOTENCY_KEY_TTL_SECONDS", idempotency_ttl)
    monkeypatch.setenv("DECKLY_CACHE_JOB_RETENTION_SECONDS", retention)


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
        clean_environment, DECKLY_SWEEP_RUNNING_STALE_AFTER_SECONDS=str(stale_after)
    )

    assert settings.sweep.running_stale_after_seconds == stale_after


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
