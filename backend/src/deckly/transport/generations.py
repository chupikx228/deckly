import re
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from deckly.application.generations import CreateGeneration, GetGeneration, JobCreated
from deckly.application.ports import IdempotencyScope
from deckly.domain.exceptions import JobNotFoundError
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import Failed, GenerationJob, JobStage, JobStatus, Succeeded
from deckly.domain.notes.note_type import NoteType
from deckly.domain.text import is_uuid_v4, visible_length
from deckly.transport.body import ResponseBody, UtcDateTime
from deckly.transport.dependencies import (
    create_generation_use_case,
    get_generation_use_case,
    problem_responder,
)
from deckly.transport.error_handlers import RETRY_AFTER_HEADER, ProblemResponder
from deckly.transport.problem import Problem
from deckly.transport.results import GenerationResultBody

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
CLIENT_ID_HEADER = "X-Client-Id"

MIN_TOPIC_LENGTH = 3
MAX_TOPIC_LENGTH = 200
MIN_CARD_COUNT = 5
MAX_CARD_COUNT = 200
MAX_INSTRUCTIONS_LENGTH = 500
NUL = "\x00"
POLL_RETRY_AFTER_SECONDS = 2

CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HYPHENATED_UUID = re.compile(CANONICAL_UUID.pattern, re.IGNORECASE)

LANGUAGE_TAG = re.compile(
    r"(?:"
    r"(?:[A-Za-z]{2,3}(?:-[A-Za-z]{3}){0,3}|[A-Za-z]{4,8})"
    r"(?:-[A-Za-z]{4})?"
    r"(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(?:-[0-9A-WYZa-wyz](?:-[A-Za-z0-9]{2,8})+)*"
    r"(?:-[Xx](?:-[A-Za-z0-9]{1,8})+)?"
    r"|[Xx](?:-[A-Za-z0-9]{1,8})+"
    r")"
)
IRREGULAR_LANGUAGE_TAGS = frozenset(
    {
        "en-gb-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "sgn-be-fr",
        "sgn-be-nl",
        "sgn-ch-de",
    }
)


def require_canonical_uuid(value: object) -> object:
    if not isinstance(value, str) or CANONICAL_UUID.fullmatch(value) is None:
        message = "must be a lowercase hyphenated UUID"
        raise ValueError(message)
    return value


def require_uuid_v4(value: UUID) -> UUID:
    if not is_uuid_v4(value):
        message = "must be a version 4 UUID"
        raise ValueError(message)
    return value


type CanonicalUuid = Annotated[UUID, BeforeValidator(require_canonical_uuid), AfterValidator(require_uuid_v4)]


def parse_job_id(value: str) -> UUID:
    if HYPHENATED_UUID.fullmatch(value) is None:
        message = f"{value!r} is not a job id"
        raise JobNotFoundError(message)
    return UUID(value)


def require_client_id(client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)]) -> UUID:
    return client_id


class GenerationRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=MIN_TOPIC_LENGTH, max_length=MAX_TOPIC_LENGTH),
    ]
    language: str
    card_count: int = Field(alias="cardCount", ge=MIN_CARD_COUNT, le=MAX_CARD_COUNT, strict=True)
    difficulty: Difficulty = Difficulty.INTERMEDIATE
    note_types: tuple[NoteType, ...] = Field(alias="noteTypes", default=(NoteType.BASIC,), min_length=1)
    include_images: bool = Field(alias="includeImages", default=False, strict=True)
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS_LENGTH)

    @field_validator("language")
    @classmethod
    def require_language_tag(cls, value: str) -> str:
        if not value.isascii() or (
            LANGUAGE_TAG.fullmatch(value) is None and value.lower() not in IRREGULAR_LANGUAGE_TAGS
        ):
            message = "language must be a well-formed BCP 47 tag"
            raise ValueError(message)
        return value

    @field_validator("topic")
    @classmethod
    def require_visible_topic(cls, value: str) -> str:
        if visible_length(value) < MIN_TOPIC_LENGTH:
            message = f"topic must contain at least {MIN_TOPIC_LENGTH} visible characters"
            raise ValueError(message)
        return value

    @field_validator("topic", "instructions")
    @classmethod
    def reject_nul(cls, value: str | None) -> str | None:
        if value is not None and NUL in value:
            message = "text must not contain NUL characters"
            raise ValueError(message)
        return value

    @field_validator("instructions", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            message = "instructions must be a string when present"
            raise ValueError(message)
        return value

    def to_domain(self) -> GenerationRequest:
        return GenerationRequest(
            topic=self.topic,
            language=self.language,
            card_count=self.card_count,
            difficulty=self.difficulty,
            note_types=self.note_types,
            include_images=self.include_images,
            instructions=self.instructions,
        )


class QuotaBody(ResponseBody):
    limit: int
    remaining: int
    resets_at: datetime = Field(alias="resetsAt")


class GenerationJobCreatedBody(ResponseBody):
    job_id: UUID = Field(alias="jobId")
    status: JobStatus
    created_at: datetime = Field(alias="createdAt")
    quota: QuotaBody

    @classmethod
    def from_created(cls, created: JobCreated) -> Self:
        return cls(
            job_id=created.job.job_id,
            status=created.job.status,
            created_at=created.job.created_at,
            quota=QuotaBody(
                limit=created.quota.limit,
                remaining=created.quota.remaining,
                resets_at=created.quota.resets_at,
            ),
        )


class GenerationJobBody(ResponseBody):
    job_id: UUID = Field(alias="jobId")
    status: JobStatus
    stage: JobStage | None
    progress: float
    created_at: UtcDateTime = Field(alias="createdAt")
    updated_at: UtcDateTime = Field(alias="updatedAt")
    result: GenerationResultBody | None
    error: Problem | None

    @classmethod
    def from_job(cls, job: GenerationJob, problems: ProblemResponder) -> Self:
        state = job.state
        return cls(
            job_id=job.job_id,
            status=job.status,
            stage=job.stage,
            progress=job.progress.value,
            created_at=job.created_at,
            updated_at=job.updated_at,
            result=GenerationResultBody.from_result(state.result) if isinstance(state, Succeeded) else None,
            error=problems.describe_failure(state.code) if isinstance(state, Failed) else None,
        )


router = APIRouter()


@router.post("/generations", status_code=HTTPStatus.ACCEPTED)
async def create_generation(
    body: GenerationRequestBody,
    idempotency_key: Annotated[CanonicalUuid, Header(alias=IDEMPOTENCY_KEY_HEADER)],
    client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)],
    create: Annotated[CreateGeneration, Depends(create_generation_use_case)],
) -> GenerationJobCreatedBody:
    scope = IdempotencyScope(client_id=client_id, idempotency_key=idempotency_key)
    return GenerationJobCreatedBody.from_created(await create(body.to_domain(), scope))


@router.get("/generations/{job_id}", dependencies=[Depends(require_client_id)])
async def get_generation(
    job_id: str,
    response: Response,
    get: Annotated[GetGeneration, Depends(get_generation_use_case)],
    problems: Annotated[ProblemResponder, Depends(problem_responder)],
) -> GenerationJobBody:
    job = await get(parse_job_id(job_id))
    if not job.is_terminal:
        response.headers[RETRY_AFTER_HEADER] = str(POLL_RETRY_AFTER_SECONDS)
    return GenerationJobBody.from_job(job, problems)
