from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from deckly.application.regeneration import RegenerateNote
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

    topic: Topic
    language: LanguageTag
    note_type: NoteType = Field(alias="noteType")
    rejected_note: RejectedNoteBody = Field(alias="rejectedNote")
    reason: RejectionReason

    def to_domain(self) -> RegenerationRequest:
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
