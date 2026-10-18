import math
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
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

from deckly.domain.generation import MAX_CARD_COUNT
from deckly.infrastructure.documents.archive import MAX_TOTAL_PART_BYTES
from deckly.infrastructure.documents.pdf import MAX_DECODED_CONTENT_BYTES, MAX_DECODED_STREAM_BYTES
from deckly.infrastructure.media.licensing import MIN_IMAGE_SIDE
from deckly.infrastructure.moderation.moderator import NOTES_PER_CONTENT_CHECK
from deckly.infrastructure.observability.tracing import TraceExporter
from deckly.infrastructure.search.parser import MIN_VISIBLE_CHARACTERS
from deckly.infrastructure.transcription.client import BYTES_PER_SECOND

ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg"
ENV_FILE = ".env"
MAX_SEARCH_RESULTS = 20
MAX_MEDIA_IMAGES = 50
MAX_MEDIA_CANDIDATES = 50
MAX_MEDIA_CONCURRENCY = 8
MAX_JUDGED_CANDIDATES = 10
WORKER_MAX_JOBS = 10
DEFAULT_MODERATION_MAX_CONCURRENCY = WORKER_MAX_JOBS
MAX_MODERATION_CONCURRENCY = 40
MAX_MODERATION_CHUNKS_PER_JOB = math.ceil(MAX_CARD_COUNT / NOTES_PER_CONTENT_CHECK)
TOPIC_GATED_STAGES_PER_JOB = 2
DOCUMENT_GATED_STAGES_PER_JOB = 3
MAX_MODERATION_TIMEOUT_SECONDS = 10
REGENERATE_OVERHEAD_SECONDS = 0.5
MEDIA_URL_MIN_VALIDITY_SECONDS = 24 * 60 * 60
MIN_JOB_RETENTION_SECONDS = 24 * 60 * 60
MINUTES_PER_HOUR = 60
SECONDS_PER_MINUTE = 60
MEBIBYTE = 1024 * 1024
MAX_DOCUMENT_BYTES = 100 * MEBIBYTE
DEFAULT_DOCUMENT_UPLOAD_MAX_CONCURRENCY = 4
MAX_DOCUMENT_UPLOAD_CONCURRENCY = 32
DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES = 1288 * MEBIBYTE
DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN = 2
DEFAULT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES = 1280 * MEBIBYTE
DOCUMENT_DOWNLOAD_PEAK_COPIES = 2
DOCUMENT_PARSE_WORKING_SET_BYTES = max(
    MAX_DECODED_CONTENT_BYTES + MAX_DECODED_STREAM_BYTES, MAX_TOTAL_PART_BYTES
)
DEFAULT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS = 120
MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS = 900
MAX_DOCUMENT_SOURCE_CHARACTERS = 200_000
DEFAULT_RECORDING_MAX_BYTES = 48 * MEBIBYTE
MAX_RECORDING_BYTES = 100 * MEBIBYTE
DEFAULT_RECORDING_MAX_DURATION_SECONDS = 15 * SECONDS_PER_MINUTE
MAX_RECORDING_DURATION_SECONDS = 4 * MINUTES_PER_HOUR * SECONDS_PER_MINUTE
DEFAULT_RECORDING_UPLOAD_MAX_CONCURRENCY = 1
MAX_RECORDING_UPLOAD_CONCURRENCY = 8
DEFAULT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS = 240
DEFAULT_RECORDING_URL_MAX_CONCURRENCY = 1
RECORDING_URL_SUBPROCESS_PEAK_BYTES = 448 * MEBIBYTE
MAX_RECORDING_URL_CONCURRENCY = 4
DEFAULT_RECORDING_URL_TIMEOUT_SECONDS = 120
MAX_RECORDING_URL_TIMEOUT_SECONDS = 600
DEFAULT_RECORDING_URL_FETCHES_PER_WINDOW = 5
DEFAULT_RECORDING_URL_FETCHES_PER_ADDRESS_PER_WINDOW = 20
DEFAULT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW = 200
DEFAULT_RECORDING_URL_WINDOW_SECONDS = 60 * 60
MAX_DOCUMENT_PARSE_DEADLINE_SECONDS = 120
MIN_DOCUMENT_RETENTION_SECONDS = 60 * 60
MAX_DOCUMENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_STORAGE_CONCURRENCY = 16
TRANSCRIPTION_WORKER_MAX_JOBS = 1
MEASURED_TRANSCRIPTION_REAL_TIME_FACTOR = 0.14
TRANSCRIPTION_DEADLINE_MARGIN = 2
TRANSCRIPTION_ENGINE_WORKING_SET_BYTES = 1024 * MEBIBYTE
TRANSCRIPTION_MEMORY_SAFETY_MARGIN = DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN
DEFAULT_TRANSCRIPTION_MODEL_DIRECTORY = Path("models/faster-whisper-small")
DEFAULT_TRANSCRIPTION_CPU_THREADS = 2
MAX_TRANSCRIPTION_CPU_THREADS = 32
DEFAULT_TRANSCRIPTION_DEADLINE_SECONDS = 540
DEFAULT_TRANSCRIPTION_DECODE_DEADLINE_SECONDS = 30
MAX_TRANSCRIPTION_DECODE_DEADLINE_SECONDS = 300
DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS = 960
DEFAULT_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES = 2560 * MEBIBYTE
DEFAULT_TRANSCRIPTION_WORKER_METRICS_PORT = 9465
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
ROOT_PATH = "/"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ModelProvider = Literal["anthropic", "deepseek", "gemini"]
MAX_PORT = 65535
FIRST_VISIBLE_ASCII = "!"
LAST_VISIBLE_ASCII = "~"
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


def is_visible_ascii(value: str) -> bool:
    return all(FIRST_VISIBLE_ASCII <= character <= LAST_VISIBLE_ASCII for character in value)


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
    document_store_timeout_seconds: PositiveSeconds

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
    media_judge_timeout_seconds: PositiveInt
    media_judge_deadline_seconds: PositiveInt
    media_judge_max_attempts: PositiveInt
    media_judged_candidates_per_note: Annotated[int, Field(ge=1, le=MAX_JUDGED_CANDIDATES)]
    moderation_model_name: str
    moderation_max_output_tokens: PositiveInt
    moderation_timeout_seconds: Annotated[
        float, Field(gt=0, le=MAX_MODERATION_TIMEOUT_SECONDS, allow_inf_nan=False)
    ]
    moderation_filter_timeout_seconds: PositiveInt
    moderation_filter_deadline_seconds: PositiveInt
    moderation_filter_max_attempts: PositiveInt
    moderation_max_concurrency: Annotated[int, Field(ge=1, le=MAX_MODERATION_CONCURRENCY)] = (
        DEFAULT_MODERATION_MAX_CONCURRENCY
    )
    storage_endpoint_url: AnyHttpUrl
    storage_bucket: str
    storage_region: str
    storage_access_key_id: str
    storage_secret_access_key: SecretStr
    storage_upload_timeout_seconds: PositiveInt
    storage_upload_deadline_seconds: PositiveInt
    storage_delete_timeout_seconds: PositiveInt
    storage_delete_deadline_seconds: PositiveInt
    storage_download_timeout_seconds: PositiveInt
    storage_download_deadline_seconds: PositiveInt
    storage_max_attempts: PositiveInt
    storage_retry_base_delay_seconds: PositiveInt
    storage_retry_max_delay_seconds: PositiveInt
    storage_circuit_failure_threshold: PositiveInt
    storage_circuit_reset_seconds: PositiveInt
    storage_max_concurrency: Annotated[int, Field(ge=1, le=MAX_STORAGE_CONCURRENCY)]

    def moderation_gate_wait_per_stage_seconds(self, concurrent_jobs: int) -> int:
        calls_ahead = concurrent_jobs * MAX_MODERATION_CHUNKS_PER_JOB - 1
        waves_ahead = calls_ahead // self.moderation_max_concurrency
        longest_slot_hold = max(self.moderation_filter_deadline_seconds, self.media_judge_deadline_seconds)
        return waves_ahead * longest_slot_hold

    @property
    def moderation_gate_worst_case_wait_per_stage_seconds(self) -> int:
        return self.moderation_gate_wait_per_stage_seconds(WORKER_MAX_JOBS)

    @property
    def moderation_gate_worst_case_wait_seconds(self) -> int:
        return TOPIC_GATED_STAGES_PER_JOB * self.moderation_gate_worst_case_wait_per_stage_seconds

    @property
    def document_moderation_gate_worst_case_wait_seconds(self) -> int:
        return DOCUMENT_GATED_STAGES_PER_JOB * self.moderation_gate_worst_case_wait_per_stage_seconds

    @property
    def recording_moderation_gate_worst_case_wait_seconds(self) -> int:
        return DOCUMENT_GATED_STAGES_PER_JOB * self.moderation_gate_wait_per_stage_seconds(
            TRANSCRIPTION_WORKER_MAX_JOBS
        )

    @field_validator("model_api_key", "search_api_key", "storage_secret_access_key")
    @classmethod
    def require_visible_ascii_key(cls, key: SecretStr) -> SecretStr:
        if not is_visible_ascii(key.get_secret_value()):
            message = "api keys may only contain visible ASCII characters"
            raise ValueError(message)
        return key

    @field_validator("storage_access_key_id", "storage_region")
    @classmethod
    def require_visible_ascii_identifier(cls, value: str) -> str:
        if not is_visible_ascii(value):
            message = "storage identifiers may only contain visible ASCII characters"
            raise ValueError(message)
        return value

    @field_validator("storage_bucket")
    @classmethod
    def require_bucket_name(cls, bucket: str) -> str:
        if BUCKET_NAME.fullmatch(bucket) is None:
            message = "the storage bucket must be 3-63 lowercase letters, digits, dots or hyphens"
            raise ValueError(message)
        return bucket

    @field_validator("storage_endpoint_url")
    @classmethod
    def require_bare_endpoint(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if (
            url.path not in {None, ROOT_PATH}
            or url.query is not None
            or url.fragment is not None
            or url.username is not None
            or url.password is not None
        ):
            message = "the storage endpoint url must not carry credentials, a path, a query or a fragment"
            raise ValueError(message)
        return url

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
    def require_media_judge_deadline_to_fit_one_attempt(self) -> Self:
        if self.media_judge_deadline_seconds < self.media_judge_timeout_seconds:
            message = "media judge deadline must be at least the timeout of a single attempt"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def require_storage_deadlines_to_fit_one_attempt(self) -> Self:
        if (
            self.storage_upload_deadline_seconds < self.storage_upload_timeout_seconds
            or self.storage_delete_deadline_seconds < self.storage_delete_timeout_seconds
            or self.storage_download_deadline_seconds < self.storage_download_timeout_seconds
        ):
            message = "storage deadlines must be at least the timeout of a single attempt"
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
    document_max_bytes: Annotated[int, Field(ge=1, le=MAX_DOCUMENT_BYTES)]
    document_uploads_per_window: PositiveInt
    document_uploads_per_address_per_window: PositiveInt
    document_upload_window_seconds: PositiveInt
    document_upload_max_concurrency: Annotated[int, Field(ge=1, le=MAX_DOCUMENT_UPLOAD_CONCURRENCY)] = (
        DEFAULT_DOCUMENT_UPLOAD_MAX_CONCURRENCY
    )
    document_upload_read_timeout_seconds: Annotated[
        int, Field(ge=1, le=MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS)
    ] = DEFAULT_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS
    document_upload_memory_budget_bytes: PositiveInt = DEFAULT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES
    document_worker_memory_budget_bytes: PositiveInt = DEFAULT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES
    document_max_source_characters: Annotated[
        int, Field(ge=MIN_VISIBLE_CHARACTERS, le=MAX_DOCUMENT_SOURCE_CHARACTERS)
    ]
    document_parse_deadline_seconds: Annotated[int, Field(ge=1, le=MAX_DOCUMENT_PARSE_DEADLINE_SECONDS)]
    recording_max_bytes: Annotated[int, Field(ge=1, le=MAX_RECORDING_BYTES)] = DEFAULT_RECORDING_MAX_BYTES
    recording_max_duration_seconds: Annotated[int, Field(ge=1, le=MAX_RECORDING_DURATION_SECONDS)] = (
        DEFAULT_RECORDING_MAX_DURATION_SECONDS
    )
    recording_upload_max_concurrency: Annotated[int, Field(ge=1, le=MAX_RECORDING_UPLOAD_CONCURRENCY)] = (
        DEFAULT_RECORDING_UPLOAD_MAX_CONCURRENCY
    )
    recording_upload_read_timeout_seconds: Annotated[
        int, Field(ge=1, le=MAX_DOCUMENT_UPLOAD_READ_TIMEOUT_SECONDS)
    ] = DEFAULT_RECORDING_UPLOAD_READ_TIMEOUT_SECONDS
    recording_url_max_concurrency: Annotated[int, Field(ge=1, le=MAX_RECORDING_URL_CONCURRENCY)] = (
        DEFAULT_RECORDING_URL_MAX_CONCURRENCY
    )
    recording_url_timeout_seconds: Annotated[int, Field(ge=1, le=MAX_RECORDING_URL_TIMEOUT_SECONDS)] = (
        DEFAULT_RECORDING_URL_TIMEOUT_SECONDS
    )
    recording_url_fetches_per_window: PositiveInt = DEFAULT_RECORDING_URL_FETCHES_PER_WINDOW
    recording_url_fetches_per_address_per_window: PositiveInt = (
        DEFAULT_RECORDING_URL_FETCHES_PER_ADDRESS_PER_WINDOW
    )
    recording_url_server_fetches_per_window: PositiveInt = DEFAULT_RECORDING_URL_SERVER_FETCHES_PER_WINDOW
    recording_url_window_seconds: PositiveInt = DEFAULT_RECORDING_URL_WINDOW_SECONDS

    @property
    def document_worker_job_peak_bytes(self) -> int:
        return max(
            DOCUMENT_DOWNLOAD_PEAK_COPIES * self.document_max_bytes,
            self.document_max_bytes + DOCUMENT_PARSE_WORKING_SET_BYTES,
        )

    @property
    def upload_worst_case_bytes(self) -> int:
        held = (
            self.document_upload_max_concurrency * self.document_max_bytes
            + (self.recording_upload_max_concurrency + self.recording_url_max_concurrency)
            * self.recording_max_bytes
            + self.recording_url_max_concurrency * RECORDING_URL_SUBPROCESS_PEAK_BYTES
        )
        return held * DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN

    @model_validator(mode="after")
    def require_address_limit_to_be_coarser(self) -> Self:
        if self.generation_jobs_per_address_per_day < self.generation_jobs_per_day:
            message = "the per-address generation limit must be at least the per-client limit"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def require_address_upload_limit_to_be_coarser(self) -> Self:
        if self.document_uploads_per_address_per_window < self.document_uploads_per_window:
            message = "the per-address document upload limit must be at least the per-client limit"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def require_recording_url_limits_to_get_coarser(self) -> Self:
        if self.recording_url_fetches_per_address_per_window < self.recording_url_fetches_per_window:
            message = "the per-address recording url fetch limit must be at least the per-client limit"
            raise ValueError(message)
        if self.recording_url_server_fetches_per_window < self.recording_url_fetches_per_address_per_window:
            message = "the server-wide recording url fetch limit must be at least the per-address limit"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def require_concurrent_uploads_to_fit_the_memory_budget(self) -> Self:
        worst_case_bytes = self.upload_worst_case_bytes
        if worst_case_bytes > self.document_upload_memory_budget_bytes:
            message = (
                f"the document upload concurrency cap ({self.document_upload_max_concurrency}) times the "
                f"document size limit ({self.document_max_bytes} bytes), plus the recording upload "
                f"concurrency cap ({self.recording_upload_max_concurrency}) and the recording url fetch "
                f"concurrency cap ({self.recording_url_max_concurrency}) times the recording size limit "
                f"({self.recording_max_bytes} bytes), plus the recording url fetch concurrency cap times "
                f"the measured yt-dlp and deno peak ({RECORDING_URL_SUBPROCESS_PEAK_BYTES} bytes), times the "
                "safety margin "
                f"({DOCUMENT_UPLOAD_MEMORY_SAFETY_MARGIN}) is {worst_case_bytes} bytes, which exceeds the "
                f"upload memory budget ({self.document_upload_memory_budget_bytes} bytes); lower "
                "DECKLY_LIMIT_DOCUMENT_UPLOAD_MAX_CONCURRENCY, DECKLY_LIMIT_DOCUMENT_MAX_BYTES, "
                "DECKLY_LIMIT_RECORDING_UPLOAD_MAX_CONCURRENCY, "
                "DECKLY_LIMIT_RECORDING_URL_MAX_CONCURRENCY or DECKLY_LIMIT_RECORDING_MAX_BYTES, or raise "
                "DECKLY_LIMIT_DOCUMENT_UPLOAD_MEMORY_BUDGET_BYTES; the check assumes a single process, so "
                "divide the budget by the number of workers or replicas sharing one container"
            )
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def require_concurrent_document_jobs_to_fit_the_worker_memory_budget(self) -> Self:
        worst_case_bytes = WORKER_MAX_JOBS * self.document_worker_job_peak_bytes
        if worst_case_bytes > self.document_worker_memory_budget_bytes:
            message = (
                f"the worker job concurrency ({WORKER_MAX_JOBS}) times the peak memory of one "
                f"document-sourced job ({self.document_worker_job_peak_bytes} bytes, the larger of "
                f"{DOCUMENT_DOWNLOAD_PEAK_COPIES} copies of the document size limit "
                f"({self.document_max_bytes} bytes) while it downloads and the size limit plus "
                f"{DOCUMENT_PARSE_WORKING_SET_BYTES} bytes of parser working set while it parses) is "
                f"{worst_case_bytes} bytes, which exceeds the worker memory budget "
                f"({self.document_worker_memory_budget_bytes} bytes); lower "
                "DECKLY_LIMIT_DOCUMENT_MAX_BYTES, or raise "
                "DECKLY_LIMIT_DOCUMENT_WORKER_MEMORY_BUDGET_BYTES; the check assumes a single worker "
                "process, so divide the budget by the number of workers sharing one container"
            )
            raise ValueError(message)
        return self


class TranscriptionSettings(BaseSettings):
    model_config = _settings_config("DECKLY_TRANSCRIPTION_")

    enabled: bool = True
    model_directory: Path = DEFAULT_TRANSCRIPTION_MODEL_DIRECTORY
    cpu_threads: Annotated[int, Field(ge=1, le=MAX_TRANSCRIPTION_CPU_THREADS)] = (
        DEFAULT_TRANSCRIPTION_CPU_THREADS
    )
    deadline_seconds: Annotated[int, Field(ge=1, le=MAX_RECORDING_DURATION_SECONDS)] = (
        DEFAULT_TRANSCRIPTION_DEADLINE_SECONDS
    )
    decode_deadline_seconds: Annotated[int, Field(ge=1, le=MAX_TRANSCRIPTION_DECODE_DEADLINE_SECONDS)] = (
        DEFAULT_TRANSCRIPTION_DECODE_DEADLINE_SECONDS
    )
    job_timeout_seconds: PositiveInt = DEFAULT_TRANSCRIPTION_JOB_TIMEOUT_SECONDS
    worker_memory_budget_bytes: PositiveInt = DEFAULT_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES


class RegenerateSettings(BaseSettings):
    model_config = _settings_config("DECKLY_REGENERATE_")

    model_timeout_seconds: PositiveSeconds
    model_max_output_tokens: PositiveInt
    search_timeout_seconds: PositiveSeconds
    search_max_results: Annotated[int, Field(ge=1, le=MAX_SEARCH_RESULTS)]
    search_max_source_characters: Annotated[int, Field(ge=MIN_VISIBLE_CHARACTERS)]
    rate_limit_timeout_seconds: PositiveSeconds
    request_moderation_timeout_seconds: PositiveSeconds
    note_moderation_timeout_seconds: PositiveSeconds

    @property
    def budgeted_seconds(self) -> float:
        return math.fsum(
            [
                self.rate_limit_timeout_seconds,
                max(self.search_timeout_seconds, self.request_moderation_timeout_seconds),
                self.model_timeout_seconds,
                self.note_moderation_timeout_seconds,
                REGENERATE_OVERHEAD_SECONDS,
            ]
        )


class CacheSettings(BaseSettings):
    model_config = _settings_config("DECKLY_CACHE_")

    redis_url: RedisDsn | None = None
    generation_result_ttl_seconds: Annotated[int, Field(gt=0, lt=MEDIA_URL_MIN_VALIDITY_SECONDS)]
    idempotency_key_ttl_seconds: PositiveInt
    job_retention_seconds: Annotated[int, Field(ge=MIN_JOB_RETENTION_SECONDS)]
    document_retention_seconds: Annotated[
        int, Field(ge=MIN_DOCUMENT_RETENTION_SECONDS, le=MAX_DOCUMENT_RETENTION_SECONDS)
    ]

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
    document_batch_size: PositiveInt

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
    transcription_worker_metrics_port: Annotated[int, Field(ge=1, le=MAX_PORT)] = (
        DEFAULT_TRANSCRIPTION_WORKER_METRICS_PORT
    )

    @model_validator(mode="after")
    def require_each_worker_its_own_metrics_port(self) -> Self:
        if self.transcription_worker_metrics_port == self.worker_metrics_port:
            message = (
                "the transcription worker and the generation worker must serve metrics on different ports"
            )
            raise ValueError(message)
        return self


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
    transcription: TranscriptionSettings

    @property
    def cache_redis_url(self) -> RedisDsn:
        if self.cache.redis_url is None:
            return self.redis.url
        return self.cache.redis_url

    @property
    def card_deadlines_seconds(self) -> int:
        return (
            self.providers.model_deadline_seconds
            + self.providers.media_deadline_seconds
            + self.providers.media_judge_deadline_seconds
            + self.providers.moderation_filter_deadline_seconds
        )

    @property
    def topic_deadlines_seconds(self) -> int:
        return self.providers.search_deadline_seconds + self.card_deadlines_seconds

    @property
    def document_deadlines_seconds(self) -> int:
        return (
            self.providers.storage_download_deadline_seconds
            + self.limits.document_parse_deadline_seconds
            + self.providers.moderation_filter_deadline_seconds
            + self.card_deadlines_seconds
        )

    @property
    def recording_deadlines_seconds(self) -> int:
        return (
            self.providers.storage_download_deadline_seconds
            + self.transcription.decode_deadline_seconds
            + self.transcription.deadline_seconds
            + self.providers.moderation_filter_deadline_seconds
            + self.card_deadlines_seconds
        )

    @property
    def transcription_deadline_floor_seconds(self) -> int:
        return math.ceil(
            self.limits.recording_max_duration_seconds
            * Decimal(str(MEASURED_TRANSCRIPTION_REAL_TIME_FACTOR))
            * TRANSCRIPTION_DEADLINE_MARGIN
        )

    @property
    def transcription_engine_peak_bytes(self) -> int:
        return (
            TRANSCRIPTION_ENGINE_WORKING_SET_BYTES
            + self.limits.recording_max_duration_seconds * BYTES_PER_SECOND
        )

    @property
    def transcription_worker_worst_case_bytes(self) -> int:
        job_peak = self.limits.recording_max_bytes + self.transcription_engine_peak_bytes
        return TRANSCRIPTION_WORKER_MAX_JOBS * job_peak * TRANSCRIPTION_MEMORY_SAFETY_MARGIN

    @property
    def longest_job_timeout_seconds(self) -> int:
        if not self.transcription.enabled:
            return self.limits.generation_job_timeout_seconds
        return max(self.limits.generation_job_timeout_seconds, self.transcription.job_timeout_seconds)

    def __post_init__(self) -> None:
        self._require_generation_budgets()
        if self.transcription.enabled:
            self._require_transcription_budgets()
        self._require_housekeeping_budgets()

    def _require_transcription_budgets(self) -> None:
        recording_deadlines = self.recording_deadlines_seconds
        recording_gate_wait = self.providers.recording_moderation_gate_worst_case_wait_seconds
        if recording_deadlines + recording_gate_wait >= self.transcription.job_timeout_seconds:
            message = (
                f"the deadlines of a recording-sourced job ({recording_deadlines}s: the document download, "
                "the audio decode, the transcription, the transcript screening and the model, media, media "
                f"judge and moderation deadlines) plus the worst-case wait for a moderation concurrency slot "
                f"at each of its {DOCUMENT_GATED_STAGES_PER_JOB} gated stages ({recording_gate_wait}s) must "
                "leave room within DECKLY_TRANSCRIPTION_JOB_TIMEOUT_SECONDS "
                f"({self.transcription.job_timeout_seconds}s)"
            )
            raise ValueError(message)
        floor = self.transcription_deadline_floor_seconds
        if self.transcription.deadline_seconds < floor:
            message = (
                f"DECKLY_TRANSCRIPTION_DEADLINE_SECONDS ({self.transcription.deadline_seconds}s) is shorter "
                f"than the recording duration cap ({self.limits.recording_max_duration_seconds}s) times the "
                f"measured real-time factor ({MEASURED_TRANSCRIPTION_REAL_TIME_FACTOR}) times "
                f"{TRANSCRIPTION_DEADLINE_MARGIN} ({floor}s), so a recording at the cap would time out even "
                "on the hardware it was measured on; raise the deadline or lower "
                "DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS"
            )
            raise ValueError(message)
        worst_case_bytes = self.transcription_worker_worst_case_bytes
        if worst_case_bytes > self.transcription.worker_memory_budget_bytes:
            message = (
                f"the transcription worker's job concurrency ({TRANSCRIPTION_WORKER_MAX_JOBS}) times the "
                "peak of one recording-sourced job (the recording size limit, "
                f"{self.limits.recording_max_bytes} bytes, held while it is transcribed, plus the "
                "transcription engine's measured "
                f"{TRANSCRIPTION_ENGINE_WORKING_SET_BYTES} bytes of working set and the duration cap's "
                f"decoded audio, {self.limits.recording_max_duration_seconds}s at {BYTES_PER_SECOND} bytes a "
                f"second) times the safety margin ({TRANSCRIPTION_MEMORY_SAFETY_MARGIN}) is "
                f"{worst_case_bytes} bytes, which exceeds the transcription worker memory budget "
                f"({self.transcription.worker_memory_budget_bytes} bytes); lower "
                "DECKLY_LIMIT_RECORDING_MAX_BYTES or DECKLY_LIMIT_RECORDING_MAX_DURATION_SECONDS, or raise "
                "DECKLY_TRANSCRIPTION_WORKER_MEMORY_BUDGET_BYTES; the check assumes a single transcription "
                "worker process, so divide the budget by the number of them sharing one container"
            )
            raise ValueError(message)

    def _require_generation_budgets(self) -> None:
        job_timeout = self.limits.generation_job_timeout_seconds
        topic_deadlines = self.topic_deadlines_seconds
        document_deadlines = self.document_deadlines_seconds
        if topic_deadlines >= job_timeout:
            message = (
                "the search, model, media, media judge and moderation deadlines together must leave room "
                "within the generation job timeout"
            )
            raise ValueError(message)
        if document_deadlines >= job_timeout:
            message = (
                "the document download, document parse and document screening deadlines plus the model, "
                "media, media judge and moderation deadlines together must leave room within the generation "
                "job timeout"
            )
            raise ValueError(message)
        gate_wait = self.providers.moderation_gate_worst_case_wait_seconds
        if topic_deadlines + gate_wait >= job_timeout:
            message = (
                f"the provider deadlines ({topic_deadlines}s) plus the worst-case wait for a moderation "
                f"concurrency slot ({gate_wait}s) must leave room within the generation job timeout"
            )
            raise ValueError(message)
        document_gate_wait = self.providers.document_moderation_gate_worst_case_wait_seconds
        if document_deadlines + document_gate_wait >= job_timeout:
            message = (
                f"the deadlines of a document-sourced job ({document_deadlines}s) plus the worst-case wait "
                f"for a moderation concurrency slot at each of its {DOCUMENT_GATED_STAGES_PER_JOB} gated "
                f"stages ({document_gate_wait}s) must leave room within the generation job timeout"
            )
            raise ValueError(message)

    def _require_housekeeping_budgets(self) -> None:
        if self.regenerate.budgeted_seconds > self.limits.regenerate_note_timeout_seconds:
            message = (
                "the regenerate rate limit, the longer of the search and request moderation, the model and "
                f"note moderation timeouts plus {REGENERATE_OVERHEAD_SECONDS}s of overhead must fit within "
                "the regenerate note timeout"
            )
            raise ValueError(message)
        document_sweep_seconds = (
            math.ceil(self.sweep.document_batch_size / self.providers.storage_max_concurrency)
            * self.providers.storage_delete_deadline_seconds
        )
        if document_sweep_seconds >= self.sweep.interval_minutes * SECONDS_PER_MINUTE:
            message = (
                f"deleting one batch of expired documents can take {document_sweep_seconds}s, which must fit "
                "within the sweep interval"
            )
            raise ValueError(message)
        interrupted_job_settled = self.longest_job_timeout_seconds + self.database.job_store_timeout_seconds
        if self.sweep.running_stale_after_seconds <= interrupted_job_settled:
            message = (
                "a running job must not go stale before the generation job timeout (or, with transcription "
                "enabled, the longer transcription job timeout) and the job store timeout have both passed"
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
        transcription=TranscriptionSettings(),
    )
