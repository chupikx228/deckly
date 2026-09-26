import logging
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from deckly.application.ports import SourceMaterial
from deckly.domain.deck import GenerationResult
from deckly.domain.exceptions import InvariantViolationError, MissingSourceError
from deckly.domain.generation import GenerationRequest
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source
from deckly.domain.text import strip_unstorable
from deckly.infrastructure.card_generator.deck import repair_deck
from deckly.infrastructure.card_generator.extraction import EMPTY_DOCUMENT, ModelDocument, extract_document
from deckly.infrastructure.card_generator.note_types import NoteTypeHandler
from deckly.infrastructure.card_generator.prompt import build_prompt
from deckly.infrastructure.card_generator.untrusted import (
    JsonObject,
    UnusableOutputError,
    as_object,
    read_labels,
)
from deckly.infrastructure.llm.client import LlmClient, LlmReply, LlmStop

logger = logging.getLogger(__name__)

FIRST_SOURCE_NUMBER = 1
MIN_CARD_COUNT = 1


class DropReason(StrEnum):
    NOT_AN_OBJECT = "not_an_object"
    UNKNOWN_TYPE = "unknown_type"
    UNREQUESTED_TYPE = "unrequested_type"
    UNSUPPORTED_TYPE = "unsupported_type"
    MISMATCHED_FIELDS = "mismatched_fields"
    INVALID_CONTENT = "invalid_content"
    MISSING_SOURCE = "missing_source"
    DUPLICATE = "duplicate"
    OVER_CARD_COUNT = "over_card_count"


class DroppedNoteError(Exception):
    def __init__(self, reason: DropReason) -> None:
        super().__init__(reason)
        self.reason = reason


def note_type_of(value: object) -> NoteType | None:
    if not isinstance(value, str):
        return None
    try:
        return NoteType(value)
    except ValueError:
        return None


def storable_source(source: Source) -> Source | None:
    title = strip_unstorable(source.title)
    if title == source.title:
        return source
    try:
        return replace(source, title=title)
    except InvariantViolationError:
        return None


def cited_sources(value: object, material: tuple[SourceMaterial, ...]) -> tuple[Source, ...]:
    if not isinstance(value, list):
        return ()
    numbers = [item for item in value if type(item) is int and FIRST_SOURCE_NUMBER <= item <= len(material)]
    sources = (storable_source(material[number - FIRST_SOURCE_NUMBER].source) for number in numbers)
    return tuple(dict.fromkeys(source for source in sources if source is not None))


def outcome_level(reply: LlmReply, document: ModelDocument) -> int:
    return logging.INFO if reply.stop is LlmStop.COMPLETE and document.complete else logging.WARNING


@dataclass(frozen=True, slots=True)
class LlmCardGenerator:
    llm: LlmClient
    new_id: Callable[[], UUID]
    handlers: Mapping[NoteType, NoteTypeHandler]

    async def generate(
        self, job_id: UUID, request: GenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> GenerationResult:
        handlers = tuple(self.handlers[kind] for kind in request.note_types if kind in self.handlers)
        if request.card_count < MIN_CARD_COUNT or not handlers or not material:
            logger.warning(
                "card_generation_skipped",
                extra={
                    "job_id": str(job_id),
                    "card_count": request.card_count,
                    "generatable_types": len(handlers),
                    "material_entries": len(material),
                },
            )
            return GenerationResult(deck=repair_deck(None, request.topic), notes=())
        reply = await self.llm.complete(build_prompt(request, material, handlers))
        document = EMPTY_DOCUMENT if reply.stop is LlmStop.REFUSED else extract_document(reply.text)
        drops: Counter[DropReason] = Counter()
        notes = self._valid_notes(document, request, material, drops)
        logger.log(
            outcome_level(reply, document),
            "card_generation_finished",
            extra={
                "job_id": str(job_id),
                "reply_stop": reply.stop,
                "document_complete": document.complete,
                "returned_notes": len(document.notes),
                "kept_notes": len(notes),
                "dropped_notes": dict(drops),
            },
        )
        return GenerationResult(deck=repair_deck(document.deck, request.topic), notes=notes)

    def _valid_notes(
        self,
        document: ModelDocument,
        request: GenerationRequest,
        material: tuple[SourceMaterial, ...],
        drops: Counter[DropReason],
    ) -> tuple[Note, ...]:
        valid: list[Note] = []
        seen: set[tuple[NoteType, NoteFields]] = set()
        for raw in document.notes:
            try:
                note = self._note(raw, request, material)
            except DroppedNoteError as error:
                drops[error.reason] += 1
                continue
            content = (note.note_type, note.fields)
            if content in seen:
                drops[DropReason.DUPLICATE] += 1
                continue
            seen.add(content)
            valid.append(note)
        excess = len(valid) - request.card_count
        if excess > 0:
            drops[DropReason.OVER_CARD_COUNT] += excess
        return tuple(valid[: request.card_count])

    def _note(self, raw: object, request: GenerationRequest, material: tuple[SourceMaterial, ...]) -> Note:
        note = as_object(raw)
        if note is None:
            raise DroppedNoteError(DropReason.NOT_AN_OBJECT)
        fields = self._fields(note, request)
        try:
            return Note(
                client_id=self.new_id(),
                fields=fields,
                sources=cited_sources(note.get("sources"), material),
                tags=read_labels(note.get("tags")),
            )
        except MissingSourceError as error:
            raise DroppedNoteError(DropReason.MISSING_SOURCE) from error
        except InvariantViolationError as error:
            raise DroppedNoteError(DropReason.INVALID_CONTENT) from error

    def _fields(self, note: JsonObject, request: GenerationRequest) -> NoteFields:
        handler = self._handler(note.get("noteType"), request)
        fields = as_object(note.get("fields"))
        if fields is None:
            raise DroppedNoteError(DropReason.MISMATCHED_FIELDS)
        try:
            return handler.parse(fields)
        except UnusableOutputError as error:
            raise DroppedNoteError(DropReason.MISMATCHED_FIELDS) from error
        except InvariantViolationError as error:
            raise DroppedNoteError(DropReason.INVALID_CONTENT) from error

    def _handler(self, value: object, request: GenerationRequest) -> NoteTypeHandler:
        note_type = note_type_of(value)
        if note_type is None:
            raise DroppedNoteError(DropReason.UNKNOWN_TYPE)
        if note_type not in request.note_types:
            raise DroppedNoteError(DropReason.UNREQUESTED_TYPE)
        handler = self.handlers.get(note_type)
        if handler is None:
            raise DroppedNoteError(DropReason.UNSUPPORTED_TYPE)
        return handler
