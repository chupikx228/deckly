import asyncio
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import batched
from types import MappingProxyType
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import ContentModerator
from deckly.domain.deck import Deck, GenerationResult
from deckly.domain.generation import GenerationRequest
from deckly.domain.notes.note import Note
from deckly.domain.regeneration import RegenerationRequest
from deckly.domain.source import SourceQuery
from deckly.infrastructure.card_generator.deck import repair_deck
from deckly.infrastructure.card_generator.extraction import first_opening
from deckly.infrastructure.card_generator.untrusted import JsonObject, as_object
from deckly.infrastructure.llm.client import LlmClient, LlmError, LlmReply, LlmStop
from deckly.infrastructure.moderation.prompt import (
    build_content_prompt,
    build_regeneration_screening_prompt,
    build_topic_prompt,
    note_number,
)
from deckly.infrastructure.provider_faults import PROVIDER_FAULT_RETRY_AFTER_SECONDS

logger = logging.getLogger(__name__)

NO_DECK_REPLACEMENT = None
CONFLICTING_VALUE = None
NOTES_PER_CONTENT_CHECK = 50
NO_NOTES: tuple[Note, ...] = ()


class Verdict(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


NO_NOTE_VERDICTS: Mapping[str, Verdict] = MappingProxyType({})


class UnusableVerdictError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ContentVerdicts:
    deck: Verdict
    notes: Mapping[str, Verdict]


REFUSED_CONTENT = ContentVerdicts(deck=Verdict.BLOCK, notes=NO_NOTE_VERDICTS)


@dataclass(frozen=True, slots=True)
class ContentCheck:
    stop: LlmStop
    verdicts: ContentVerdicts

    def judged(self, notes: Sequence[Note]) -> list[Verdict | None]:
        return [self.verdicts.notes.get(note_number(index)) for index in range(len(notes))]


@contextmanager
def moderation_faults_as_upstream(context: Mapping[str, str]) -> Iterator[None]:
    try:
        yield
    except (LlmError, UnusableVerdictError) as error:
        logger.exception("moderation_failed", extra={**context, "error": type(error).__name__})
        raise UpstreamUnavailableError(PROVIDER_FAULT_RETRY_AFTER_SECONDS) from error


def verdict_of(value: object) -> Verdict | None:
    if not isinstance(value, str):
        return None
    try:
        return Verdict(value.strip().lower())
    except ValueError:
        return None


def without_conflicts(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        document[key] = value if key not in document or document[key] == value else CONFLICTING_VALUE
    return document


VERDICT_DECODER = json.JSONDecoder(object_pairs_hook=without_conflicts, strict=False)


def decoded_reply(text: str) -> object:
    start = first_opening(text)
    if start < 0:
        return None
    try:
        value: object = VERDICT_DECODER.raw_decode(text, start)[0]
    except (ValueError, RecursionError):
        return None
    return value


def reply_object(reply: LlmReply) -> JsonObject:
    document = as_object(decoded_reply(reply.text))
    if document is None:
        message = f"moderation reply is not a JSON object (stop: {reply.stop})"
        raise UnusableVerdictError(message)
    return document


def topic_verdict(reply: LlmReply) -> Verdict:
    if reply.stop is LlmStop.REFUSED:
        return Verdict.BLOCK
    verdict = verdict_of(reply_object(reply).get("verdict"))
    if verdict is None:
        message = "moderation reply carries no topic verdict"
        raise UnusableVerdictError(message)
    return verdict


def content_verdicts(reply: LlmReply) -> ContentVerdicts:
    if reply.stop is LlmStop.REFUSED:
        return REFUSED_CONTENT
    document = reply_object(reply)
    notes = as_object(document.get("notes"))
    if notes is None:
        message = "moderation reply carries no note verdicts"
        raise UnusableVerdictError(message)
    verdicts = {
        number: verdict for number, value in notes.items() if (verdict := verdict_of(value)) is not None
    }
    return ContentVerdicts(deck=verdict_of(document.get("deck")) or Verdict.BLOCK, notes=verdicts)


@dataclass(frozen=True, slots=True)
class LlmTopicModerator:
    llm: LlmClient

    async def allows(self, request: GenerationRequest) -> bool:
        with moderation_faults_as_upstream({"subject": "topic"}):
            reply = await self.llm.complete(build_topic_prompt(request))
            verdict = topic_verdict(reply)
        logger.info("topic_screened", extra={"verdict": verdict, "reply_stop": reply.stop})
        return verdict is Verdict.ALLOW


@dataclass(frozen=True, slots=True)
class LlmContentModerator:
    llm: LlmClient

    async def screen(self, _job_id: UUID, query: SourceQuery, result: GenerationResult) -> GenerationResult:
        batches = tuple(batched(result.notes, NOTES_PER_CONTENT_CHECK)) or (NO_NOTES,)
        checks = await self._check_all(result.deck, batches)
        judged = [
            verdict for check, batch in zip(checks, batches, strict=True) for verdict in check.judged(batch)
        ]
        kept = tuple(
            note for note, verdict in zip(result.notes, judged, strict=True) if verdict is Verdict.ALLOW
        )
        deck_allowed = all(check.verdicts.deck is Verdict.ALLOW for check in checks)
        logger.log(
            logging.INFO if len(kept) == len(result.notes) and deck_allowed else logging.WARNING,
            "content_screened",
            extra={
                "reply_stops": [check.stop for check in checks],
                "screened_notes": len(result.notes),
                "kept_notes": len(kept),
                "blocked_notes": judged.count(Verdict.BLOCK),
                "unjudged_notes": judged.count(None),
                "deck_replaced": not deck_allowed,
            },
        )
        deck = result.deck if deck_allowed else repair_deck(NO_DECK_REPLACEMENT, query.topic)
        return GenerationResult(deck=deck, notes=kept)

    async def _check_all(self, deck: Deck, batches: tuple[tuple[Note, ...], ...]) -> tuple[ContentCheck, ...]:
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._check(deck, batch)) for batch in batches]
        except BaseExceptionGroup as failures:
            first = failures.exceptions[0]
            raise first from first.__cause__
        return tuple(task.result() for task in tasks)

    async def _check(self, deck: Deck, notes: tuple[Note, ...]) -> ContentCheck:
        with moderation_faults_as_upstream({"subject": "content"}):
            reply = await self.llm.complete(build_content_prompt(GenerationResult(deck=deck, notes=notes)))
            return ContentCheck(stop=reply.stop, verdicts=content_verdicts(reply))


@dataclass(frozen=True, slots=True)
class LlmRegenerationModerator:
    llm: LlmClient
    content: ContentModerator

    async def allows_request(self, request: RegenerationRequest) -> bool:
        with moderation_faults_as_upstream({"subject": "regeneration_request"}):
            reply = await self.llm.complete(build_regeneration_screening_prompt(request))
            verdict = topic_verdict(reply)
        logger.info("regeneration_request_screened", extra={"verdict": verdict, "reply_stop": reply.stop})
        return verdict is Verdict.ALLOW

    async def allows_note(self, request_id: UUID, query: SourceQuery, note: Note) -> bool:
        result = GenerationResult(deck=repair_deck(NO_DECK_REPLACEMENT, query.topic), notes=(note,))
        screened = await self.content.screen(request_id, query, result)
        return note in screened.notes
