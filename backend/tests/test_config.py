import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from deckly.config import DatabaseSettings, ProviderSettings, load_settings

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
    "DECKLY_PROVIDER_SEARCH_API_KEY": "key",
    "DECKLY_PROVIDER_SEARCH_TIMEOUT_SECONDS": "15",
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
@pytest.mark.parametrize("variable", ["DECKLY_PROVIDER_MODEL_API_KEY", "DECKLY_PROVIDER_MODEL_NAME"])
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
