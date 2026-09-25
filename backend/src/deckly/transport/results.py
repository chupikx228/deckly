from typing import Self
from uuid import UUID

from pydantic import Field

from deckly.domain.deck import Deck, GenerationResult
from deckly.domain.media import Media, MediaKind
from deckly.domain.notes.basic import BasicOptionalReversedFields, FrontBackFields
from deckly.domain.notes.cloze import ClozeFields
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.image_occlusion import ImageOcclusionFields, OcclusionRegion
from deckly.domain.notes.multiple_choice import MultipleChoiceFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source
from deckly.transport.body import ResponseBody, UtcDateTime, is_none


class FrontBackFieldsBody(ResponseBody):
    front: str
    back: str


class OptionalReversedFieldsBody(ResponseBody):
    front: str
    back: str
    add_reverse: bool = Field(alias="addReverse")


class ClozeFieldsBody(ResponseBody):
    text: str
    extra: str


class MultipleChoiceFieldsBody(ResponseBody):
    question: str
    answer: str
    distractors: tuple[str, ...]


class OcclusionRegionBody(ResponseBody):
    ordinal: int
    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_region(cls, region: OcclusionRegion) -> Self:
        return cls(ordinal=region.ordinal, x=region.x, y=region.y, width=region.width, height=region.height)


class ImageOcclusionFieldsBody(ResponseBody):
    image_id: str = Field(alias="imageId")
    regions: tuple[OcclusionRegionBody, ...]
    extra: str


type FieldsBody = (
    FrontBackFieldsBody
    | OptionalReversedFieldsBody
    | ClozeFieldsBody
    | MultipleChoiceFieldsBody
    | ImageOcclusionFieldsBody
)


def fields_body(fields: NoteFields) -> FieldsBody:
    match fields:
        case BasicOptionalReversedFields():
            return OptionalReversedFieldsBody(
                front=fields.front, back=fields.back, add_reverse=fields.add_reverse
            )
        case FrontBackFields():
            return FrontBackFieldsBody(front=fields.front, back=fields.back)
        case ClozeFields():
            return ClozeFieldsBody(text=fields.text, extra=fields.extra)
        case MultipleChoiceFields():
            return MultipleChoiceFieldsBody(
                question=fields.question, answer=fields.answer, distractors=fields.distractors
            )
        case ImageOcclusionFields():
            return ImageOcclusionFieldsBody(
                image_id=fields.image_id,
                regions=tuple(OcclusionRegionBody.from_region(region) for region in fields.regions),
                extra=fields.extra,
            )
    message = f"{type(fields).__name__} has no wire shape"
    raise TypeError(message)


class MediaBody(ResponseBody):
    media_id: UUID = Field(alias="mediaId")
    kind: MediaKind
    url: str
    alt: str | None = Field(default=None, exclude_if=is_none)
    width: int | None = Field(default=None, exclude_if=is_none)
    height: int | None = Field(default=None, exclude_if=is_none)
    license: str

    @classmethod
    def from_media(cls, media: Media) -> Self:
        return cls(
            media_id=media.media_id,
            kind=media.kind,
            url=media.url,
            alt=media.alt,
            width=media.width,
            height=media.height,
            license=media.license,
        )


class SourceBody(ResponseBody):
    title: str
    url: str
    retrieved_at: UtcDateTime | None = Field(default=None, alias="retrievedAt", exclude_if=is_none)

    @classmethod
    def from_source(cls, source: Source) -> Self:
        return cls(title=source.title, url=source.url, retrieved_at=source.retrieved_at)


class NoteBody(ResponseBody):
    client_id: UUID = Field(alias="clientId")
    note_type: NoteType = Field(alias="noteType")
    fields: FieldsBody
    media: tuple[MediaBody, ...]
    sources: tuple[SourceBody, ...]
    tags: tuple[str, ...]

    @classmethod
    def from_note(cls, note: Note) -> Self:
        return cls(
            client_id=note.client_id,
            note_type=note.note_type,
            fields=fields_body(note.fields),
            media=tuple(MediaBody.from_media(item) for item in note.media),
            sources=tuple(SourceBody.from_source(source) for source in note.sources),
            tags=note.tags,
        )


class DeckBody(ResponseBody):
    title: str
    description: str | None = Field(default=None, exclude_if=is_none)
    tags: tuple[str, ...]

    @classmethod
    def from_deck(cls, deck: Deck) -> Self:
        return cls(title=deck.title, description=deck.description, tags=deck.tags)


class GenerationResultBody(ResponseBody):
    deck: DeckBody
    notes: tuple[NoteBody, ...]

    @classmethod
    def from_result(cls, result: GenerationResult) -> Self:
        return cls(
            deck=DeckBody.from_deck(result.deck),
            notes=tuple(NoteBody.from_note(note) for note in result.notes),
        )
