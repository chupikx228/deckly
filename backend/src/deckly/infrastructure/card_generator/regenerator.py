import logging
from collections import Counter
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from uuid import UUID

from deckly.application.exceptions import NoValidContentError
from deckly.application.ports import SourceMaterial
from deckly.domain.exceptions import InvalidGenerationRequestError, InvariantViolationError
from deckly.domain.notes.note import Note
from deckly.domain.notes.note_type import NoteType
from deckly.domain.regeneration import RegenerationRequest
from deckly.infrastructure.card_generator.extraction import EMPTY_DOCUMENT, ModelDocument, extract_document
from deckly.infrastructure.card_generator.generator import (
    DroppedNoteError,
    DropReason,
    NoteDrafter,
    outcome_level,
)
from deckly.infrastructure.card_generator.note_types import NoteTypeHandler
from deckly.infrastructure.card_generator.regeneration_prompt import build_regeneration_prompt
from deckly.infrastructure.card_generator.untrusted import UnusableOutputError
from deckly.infrastructure.llm.client import LlmClient, LlmStop

logger = logging.getLogger(__name__)


def duplicate_key_of(handler: NoteTypeHandler, fields: Mapping[str, object]) -> Hashable | None:
    try:
        return handler.parse(fields).duplicate_key
    except (UnusableOutputError, InvariantViolationError):
        return None


@dataclass(frozen=True, slots=True)
class LlmNoteRegenerator:
    llm: LlmClient
    new_id: Callable[[], UUID]
    handlers: Mapping[NoteType, NoteTypeHandler]

    async def regenerate(
        self, request_id: UUID, request: RegenerationRequest, material: tuple[SourceMaterial, ...]
    ) -> Note:
        handler = self.handlers.get(request.note_type)
        if handler is None:
            message = f"note type {request.note_type} cannot be regenerated yet"
            raise InvalidGenerationRequestError(message)
        if not material:
            logger.warning("note_regeneration_skipped", extra={"request_id": str(request_id)})
            message = "no source material to cite"
            raise NoValidContentError(message)
        reply = await self.llm.complete(build_regeneration_prompt(request, material, handler))
        document = EMPTY_DOCUMENT if reply.stop is LlmStop.REFUSED else extract_document(reply.text)
        drops: Counter[DropReason] = Counter()
        note = self._first_valid(document, request, material, drops)
        logger.log(
            outcome_level(reply, document) if note is not None else logging.WARNING,
            "note_regeneration_finished",
            extra={
                "request_id": str(request_id),
                "note_type": request.note_type,
                "reason": request.reason,
                "reply_stop": reply.stop,
                "document_complete": document.complete,
                "returned_notes": len(document.notes),
                "kept": note is not None,
                "dropped_notes": dict(drops),
            },
        )
        if note is None:
            message = "every regenerated note was dropped during validation"
            raise NoValidContentError(message)
        return note

    def _first_valid(
        self,
        document: ModelDocument,
        request: RegenerationRequest,
        material: tuple[SourceMaterial, ...],
        drops: Counter[DropReason],
    ) -> Note | None:
        drafter = NoteDrafter(new_id=self.new_id, handlers=self.handlers)
        rejected = duplicate_key_of(self.handlers[request.note_type], request.rejected_fields)
        for raw in document.notes:
            try:
                draft = drafter.draft(raw, (request.note_type,), material)
            except DroppedNoteError as error:
                drops[error.reason] += 1
                continue
            if rejected is not None and draft.note.fields.duplicate_key == rejected:
                drops[DropReason.DUPLICATE] += 1
                continue
            return draft.note
        return None
