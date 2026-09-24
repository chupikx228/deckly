from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    TOPIC_REJECTED = "TOPIC_REJECTED"
    RATE_LIMITED = "RATE_LIMITED"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_ALREADY_TERMINAL = "JOB_ALREADY_TERMINAL"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    GENERATION_FAILED = "GENERATION_FAILED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Problem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    title: str
    status: int
    code: ErrorCode
    detail: str | None = None
    retry_after_seconds: int | None = Field(default=None, serialization_alias="retryAfterSeconds")
