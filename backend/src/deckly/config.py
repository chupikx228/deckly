from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    Field,
    NonNegativeInt,
    PositiveInt,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from deckly.infrastructure.search.parser import MIN_VISIBLE_CHARACTERS

ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg"
ENV_FILE = ".env"
MAX_SEARCH_RESULTS = 20

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ModelProvider = Literal["anthropic", "deepseek"]


def _settings_config(env_prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=env_prefix,
        env_file=ENV_FILE,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        str_strip_whitespace=True,
        str_min_length=1,
    )


class AppSettings(BaseSettings):
    model_config = _settings_config("DECKLY_")

    version: str
    log_level: LogLevel
    problem_type_base_url: AnyHttpUrl


class DatabaseSettings(BaseSettings):
    model_config = _settings_config("DECKLY_DATABASE_")

    url: PostgresDsn
    pool_size: PositiveInt
    max_overflow: NonNegativeInt
    pool_timeout_seconds: PositiveInt

    @field_validator("url")
    @classmethod
    def require_async_driver(cls, url: PostgresDsn) -> PostgresDsn:
        if url.scheme != ASYNC_POSTGRES_SCHEME:
            message = f"database url must use the {ASYNC_POSTGRES_SCHEME} scheme"
            raise ValueError(message)
        return url


class RedisSettings(BaseSettings):
    model_config = _settings_config("DECKLY_REDIS_")

    url: RedisDsn
    connect_timeout_seconds: PositiveInt
    connect_retries: NonNegativeInt


class ProviderSettings(BaseSettings):
    model_config = _settings_config("DECKLY_PROVIDER_")

    model_provider: ModelProvider
    model_base_url: AnyHttpUrl
    model_api_key: SecretStr
    model_name: str
    model_max_output_tokens: PositiveInt
    model_timeout_seconds: PositiveInt
    model_deadline_seconds: PositiveInt
    model_max_attempts: PositiveInt
    model_retry_base_delay_seconds: PositiveInt
    model_retry_max_delay_seconds: PositiveInt
    model_circuit_failure_threshold: PositiveInt
    model_circuit_reset_seconds: PositiveInt
    search_base_url: AnyHttpUrl
    search_api_key: SecretStr
    search_timeout_seconds: PositiveInt
    search_deadline_seconds: PositiveInt
    search_max_attempts: PositiveInt
    search_retry_base_delay_seconds: PositiveInt
    search_retry_max_delay_seconds: PositiveInt
    search_circuit_failure_threshold: PositiveInt
    search_circuit_reset_seconds: PositiveInt
    search_max_results: Annotated[int, Field(ge=1, le=MAX_SEARCH_RESULTS)]
    search_max_source_characters: Annotated[int, Field(ge=MIN_VISIBLE_CHARACTERS)]

    @model_validator(mode="after")
    def require_deadline_to_fit_one_attempt(self) -> Self:
        if self.model_deadline_seconds < self.model_timeout_seconds:
            message = "model deadline must be at least the timeout of a single attempt"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def require_search_deadline_to_fit_one_attempt(self) -> Self:
        if self.search_deadline_seconds < self.search_timeout_seconds:
            message = "search deadline must be at least the timeout of a single attempt"
            raise ValueError(message)
        return self


class LimitSettings(BaseSettings):
    model_config = _settings_config("DECKLY_LIMIT_")

    generation_jobs_per_day: PositiveInt
    generation_job_timeout_seconds: PositiveInt
    regenerate_note_timeout_seconds: PositiveInt


class CacheSettings(BaseSettings):
    model_config = _settings_config("DECKLY_CACHE_")

    generation_result_ttl_seconds: PositiveInt
    idempotency_key_ttl_seconds: PositiveInt
    job_retention_seconds: PositiveInt


@dataclass(frozen=True, slots=True)
class Settings:
    app: AppSettings
    database: DatabaseSettings
    redis: RedisSettings
    providers: ProviderSettings
    limits: LimitSettings
    cache: CacheSettings

    def __post_init__(self) -> None:
        provider_deadlines = self.providers.search_deadline_seconds + self.providers.model_deadline_seconds
        if provider_deadlines >= self.limits.generation_job_timeout_seconds:
            message = (
                "the search and model deadlines together must leave room within the generation job timeout"
            )
            raise ValueError(message)


def load_settings() -> Settings:
    return Settings(
        app=AppSettings(),
        database=DatabaseSettings(),
        redis=RedisSettings(),
        providers=ProviderSettings(),
        limits=LimitSettings(),
        cache=CacheSettings(),
    )
