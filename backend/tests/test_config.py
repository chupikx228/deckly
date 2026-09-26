import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from deckly.config import (
    MAX_MEDIA_CANDIDATES,
    MAX_MEDIA_CONCURRENCY,
    MAX_MEDIA_IMAGES,
    AppSettings,
    CacheSettings,
    DatabaseSettings,
    LimitSettings,
    ProviderSettings,
    RedisSettings,
    Settings,
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
}
LIMIT_ENVIRONMENT = {
    "DECKLY_LIMIT_GENERATION_JOBS_PER_DAY": "20",
    "DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS": "300",
    "DECKLY_LIMIT_REGENERATE_NOTE_TIMEOUT_SECONDS": "10",
}


def set_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PROVIDER_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("provider", ["anthropic", "deepseek"])
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


def settings_with_job_timeout(monkeypatch: pytest.MonkeyPatch, job_timeout_seconds: int) -> Settings:
    set_provider_environment(monkeypatch)
    for name, value in LIMIT_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DECKLY_LIMIT_GENERATION_JOB_TIMEOUT_SECONDS", str(job_timeout_seconds))
    return Settings(
        app=AppSettings.model_construct(),
        database=DatabaseSettings.model_construct(),
        redis=RedisSettings.model_construct(),
        providers=ProviderSettings(),
        limits=LimitSettings(),
        cache=CacheSettings.model_construct(),
    )


@pytest.mark.parametrize("job_timeout_seconds", [300, 299, 1])
def test_search_model_and_media_deadlines_that_fill_the_job_timeout_are_rejected(
    clean_environment: pytest.MonkeyPatch, job_timeout_seconds: int
) -> None:
    with pytest.raises(ValueError, match="media deadlines"):
        settings_with_job_timeout(clean_environment, job_timeout_seconds)


@pytest.mark.parametrize("job_timeout_seconds", [281, 290])
def test_media_deadline_that_pushes_the_total_past_the_job_timeout_is_rejected(
    clean_environment: pytest.MonkeyPatch, job_timeout_seconds: int
) -> None:
    with pytest.raises(ValueError, match="media deadlines"):
        settings_with_job_timeout(clean_environment, job_timeout_seconds)


def test_search_model_and_media_deadlines_that_leave_room_in_the_job_timeout_are_accepted(
    clean_environment: pytest.MonkeyPatch,
) -> None:
    settings = settings_with_job_timeout(clean_environment, 301)

    assert settings.limits.generation_job_timeout_seconds == 301


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
