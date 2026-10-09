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

from deckly.infrastructure.media.licensing import MIN_IMAGE_SIDE
from deckly.infrastructure.observability.tracing import TraceExporter
from deckly.infrastructure.search.parser import MIN_VISIBLE_CHARACTERS

ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg"
ENV_FILE = ".env"
MAX_SEARCH_RESULTS = 20
MAX_MEDIA_IMAGES = 50
MAX_MEDIA_CANDIDATES = 50
MAX_MEDIA_CONCURRENCY = 8
MAX_MODERATION_TIMEOUT_SECONDS = 10
REGENERATE_OVERHEAD_SECONDS = 1.5
MEDIA_URL_MIN_VALIDITY_SECONDS = 24 * 60 * 60
MIN_JOB_RETENTION_SECONDS = 24 * 60 * 60
MINUTES_PER_HOUR = 60

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ModelProvider = Literal["anthropic", "deepseek", "gemini"]
MAX_PORT = 65535
FIRST_VISIBLE_ASCII = "!"
LAST_VISIBLE_ASCII = "~"
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


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
    health_check_timeout_seconds: PositiveSeconds
    job_store_timeout_seconds: PositiveSeconds

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
    media_base_url: AnyHttpUrl
    media_user_agent: str
    media_timeout_seconds: PositiveInt
    media_deadline_seconds: PositiveInt
    media_max_attempts: PositiveInt
    media_retry_base_delay_seconds: PositiveInt
    media_retry_max_delay_seconds: PositiveInt
    media_circuit_failure_threshold: PositiveInt
    media_circuit_reset_seconds: PositiveInt
    media_max_images: Annotated[int, Field(ge=1, le=MAX_MEDIA_IMAGES)]
    media_candidates_per_query: Annotated[int, Field(ge=1, le=MAX_MEDIA_CANDIDATES)]
    media_thumbnail_width: Annotated[int, Field(ge=MIN_IMAGE_SIDE)]
    media_max_concurrency: Annotated[int, Field(ge=1, le=MAX_MEDIA_CONCURRENCY)]
    moderation_model_name: str
    moderation_max_output_tokens: PositiveInt
    moderation_timeout_seconds: Annotated[
        float, Field(gt=0, le=MAX_MODERATION_TIMEOUT_SECONDS, allow_inf_nan=False)
    ]
    moderation_filter_timeout_seconds: PositiveInt
    moderation_filter_deadline_seconds: PositiveInt
    moderation_filter_max_attempts: PositiveInt

    @field_validator("model_api_key", "search_api_key")
    @classmethod
    def require_visible_ascii_key(cls, key: SecretStr) -> SecretStr:
        if not all(
            FIRST_VISIBLE_ASCII <= character <= LAST_VISIBLE_ASCII for character in key.get_secret_value()
        ):
            message = "api keys may only contain visible ASCII characters"
            raise ValueError(message)
        return key

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

    @model_validator(mode="after")
    def require_media_deadline_to_fit_one_attempt(self) -> Self:
        if self.media_deadline_seconds < self.media_timeout_seconds:
            message = "media deadline must be at least the timeout of a single attempt"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def require_moderation_deadline_to_fit_one_attempt(self) -> Self:
        if self.moderation_filter_deadline_seconds < self.moderation_filter_timeout_seconds:
            message = "moderation filter deadline must be at least the timeout of a single attempt"
            raise ValueError(message)
        return self


class LimitSettings(BaseSettings):
    model_config = _settings_config("DECKLY_LIMIT_")

    generation_jobs_per_day: PositiveInt
    generation_jobs_per_address_per_day: PositiveInt
    generation_job_timeout_seconds: PositiveInt
    regenerate_note_timeout_seconds: PositiveInt
    note_regenerations_per_window: PositiveInt
    note_regeneration_window_seconds: PositiveInt

    @model_validator(mode="after")
    def require_address_limit_to_be_coarser(self) -> Self:
        if self.generation_jobs_per_address_per_day < self.generation_jobs_per_day:
            message = "the per-address generation limit must be at least the per-client limit"
            raise ValueError(message)
        return self


class RegenerateSettings(BaseSettings):
    model_config = _settings_config("DECKLY_REGENERATE_")

    model_timeout_seconds: PositiveSeconds
    model_max_output_tokens: PositiveInt
    search_timeout_seconds: PositiveSeconds
    search_max_results: Annotated[int, Field(ge=1, le=MAX_SEARCH_RESULTS)]
    search_max_source_characters: Annotated[int, Field(ge=MIN_VISIBLE_CHARACTERS)]
    rate_limit_timeout_seconds: PositiveSeconds

    @property
    def budgeted_seconds(self) -> float:
        return (
            self.rate_limit_timeout_seconds
            + self.search_timeout_seconds
            + self.model_timeout_seconds
            + REGENERATE_OVERHEAD_SECONDS
        )


class CacheSettings(BaseSettings):
    model_config = _settings_config("DECKLY_CACHE_")

    generation_result_ttl_seconds: Annotated[int, Field(gt=0, lt=MEDIA_URL_MIN_VALIDITY_SECONDS)]
    idempotency_key_ttl_seconds: PositiveInt
    job_retention_seconds: Annotated[int, Field(ge=MIN_JOB_RETENTION_SECONDS)]

    @model_validator(mode="after")
    def require_idempotency_keys_to_expire_before_their_jobs(self) -> Self:
        if self.idempotency_key_ttl_seconds > self.job_retention_seconds:
            message = "the idempotency key ttl must not exceed the job retention"
            raise ValueError(message)
        return self


class SweepSettings(BaseSettings):
    model_config = _settings_config("DECKLY_SWEEP_")

    interval_minutes: Annotated[int, Field(ge=1, le=MINUTES_PER_HOUR)]
    running_stale_after_seconds: PositiveInt
    queued_stale_after_seconds: PositiveInt
    batch_size: PositiveInt

    @field_validator("interval_minutes")
    @classmethod
    def require_interval_to_divide_the_hour(cls, interval_minutes: int) -> int:
        if MINUTES_PER_HOUR % interval_minutes != 0:
            message = f"the sweep interval must divide {MINUTES_PER_HOUR} minutes evenly"
            raise ValueError(message)
        return interval_minutes

    @model_validator(mode="after")
    def require_queued_jobs_to_wait_at_least_as_long_as_running_ones(self) -> Self:
        if self.queued_stale_after_seconds < self.running_stale_after_seconds:
            message = "a queued job must not go stale sooner than a running one"
            raise ValueError(message)
        return self


class ObservabilitySettings(BaseSettings):
    model_config = _settings_config("DECKLY_OBSERVABILITY_")

    trace_exporter: TraceExporter
    worker_metrics_host: str
    worker_metrics_port: Annotated[int, Field(ge=1, le=MAX_PORT)]


@dataclass(frozen=True, slots=True)
class Settings:
    app: AppSettings
    database: DatabaseSettings
    redis: RedisSettings
    providers: ProviderSettings
    limits: LimitSettings
    cache: CacheSettings
    regenerate: RegenerateSettings
    sweep: SweepSettings
    observability: ObservabilitySettings

    def __post_init__(self) -> None:
        provider_deadlines = (
            self.providers.search_deadline_seconds
            + self.providers.model_deadline_seconds
            + self.providers.media_deadline_seconds
            + self.providers.moderation_filter_deadline_seconds
        )
        if provider_deadlines >= self.limits.generation_job_timeout_seconds:
            message = (
                "the search, model, media and moderation deadlines together must leave room within the "
                "generation job timeout"
            )
            raise ValueError(message)
        if self.regenerate.budgeted_seconds > self.limits.regenerate_note_timeout_seconds:
            message = (
                "the regenerate rate limit, search and model timeouts plus "
                f"{REGENERATE_OVERHEAD_SECONDS}s of overhead must fit within the regenerate note timeout"
            )
            raise ValueError(message)
        interrupted_job_settled = (
            self.limits.generation_job_timeout_seconds + self.database.job_store_timeout_seconds
        )
        if self.sweep.running_stale_after_seconds <= interrupted_job_settled:
            message = (
                "a running job must not go stale before the generation job timeout and the job store "
                "timeout have both passed"
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
        regenerate=RegenerateSettings(),
        sweep=SweepSettings(),
        observability=ObservabilitySettings(),
    )
