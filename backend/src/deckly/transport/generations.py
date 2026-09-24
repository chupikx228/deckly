import re
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, field_validator

from deckly.application.generations import CreateGeneration, JobCreated
from deckly.application.ports import IdempotencyScope
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import JobStatus
from deckly.domain.notes.note_type import NoteType
from deckly.transport.dependencies import create_generation_use_case

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
CLIENT_ID_HEADER = "X-Client-Id"

MIN_TOPIC_LENGTH = 3
MAX_TOPIC_LENGTH = 200
MIN_CARD_COUNT = 5
MAX_CARD_COUNT = 200
MAX_INSTRUCTIONS_LENGTH = 500
NUL = "\x00"

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


class GenerationRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=MIN_TOPIC_LENGTH, max_length=MAX_TOPIC_LENGTH)
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


class ResponseBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True, validate_by_alias=True)


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


router = APIRouter()


@router.post("/generations", status_code=HTTPStatus.ACCEPTED)
async def create_generation(
    body: GenerationRequestBody,
    idempotency_key: Annotated[UUID, Header(alias=IDEMPOTENCY_KEY_HEADER)],
    client_id: Annotated[UUID, Header(alias=CLIENT_ID_HEADER)],
    create: Annotated[CreateGeneration, Depends(create_generation_use_case)],
) -> GenerationJobCreatedBody:
    scope = IdempotencyScope(client_id=client_id, idempotency_key=idempotency_key)
    return GenerationJobCreatedBody.from_created(await create(body.to_domain(), scope))
