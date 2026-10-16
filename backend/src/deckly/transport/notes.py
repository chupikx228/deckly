from typing import Annotated, Self

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from deckly.application.regeneration import RegenerateNote
from deckly.domain.exceptions import DocumentRegenerationUnsupportedError
from deckly.domain.notes.note_type import NoteType
from deckly.domain.regeneration import RegenerationRequest, RejectionReason
from deckly.transport.dependencies import regenerate_note_use_case
from deckly.transport.generations import CLIENT_ID_HEADER, CanonicalUuid, LanguageTag, Topic
from deckly.transport.results import NoteBody


class RejectedNoteBody(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    fields: dict[str, JsonValue]


class RegenerateNoteRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: Topic | None = None
    document_id: CanonicalUuid | None = Field(alias="documentId", default=None)
    language: LanguageTag
    note_type: NoteType = Field(alias="noteType")
    rejected_note: RejectedNoteBody = Field(alias="rejectedNote")
    reason: RejectionReason

    @field_validator("topic", "document_id", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            message = "must not be null when present; leave the field out instead"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> Self:
        if (self.topic is None) == (self.document_id is None):
            message = "exactly one of topic and documentId is required"
            raise ValueError(message)
        return self

    def to_domain(self) -> RegenerationRequest:
        if self.topic is None:
            message = "a note of a deck generated from a document cannot be regenerated in v1"
            raise DocumentRegenerationUnsupportedError(message)
        return RegenerationRequest(
            topic=self.topic,
            language=self.language,
            note_type=self.note_type,
            rejected_fields=self.rejected_note.fields,
            reason=self.reason,
        )


router = APIRouter()


@router.post("/notes/regenerate")
async def regenerate_note(
    body: RegenerateNoteRequestBody,
    client_id: Annotated[CanonicalUuid, Header(alias=CLIENT_ID_HEADER)],
    regenerate: Annotated[RegenerateNote, Depends(regenerate_note_use_case)],
) -> NoteBody:
    return NoteBody.from_note(await regenerate(body.to_domain(), client_id))
