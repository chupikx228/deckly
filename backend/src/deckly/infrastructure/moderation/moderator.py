import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.domain.deck import GenerationResult
from deckly.domain.generation import GenerationRequest
from deckly.infrastructure.card_generator.deck import repair_deck
from deckly.infrastructure.card_generator.extraction import first_opening
from deckly.infrastructure.card_generator.untrusted import JsonObject, as_object
from deckly.infrastructure.llm.client import LlmClient, LlmError, LlmReply, LlmStop
from deckly.infrastructure.moderation.prompt import build_content_prompt, build_topic_prompt, note_number
from deckly.infrastructure.provider_faults import PROVIDER_FAULT_RETRY_AFTER_SECONDS

logger = logging.getLogger(__name__)

NO_DECK_REPLACEMENT = None
CONFLICTING_VALUE = None


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

    async def screen(
        self, job_id: UUID, request: GenerationRequest, result: GenerationResult
    ) -> GenerationResult:
        with moderation_faults_as_upstream({"job_id": str(job_id), "subject": "content"}):
            reply = await self.llm.complete(build_content_prompt(result))
            verdicts = content_verdicts(reply)
        judged = [verdicts.notes.get(note_number(index)) for index in range(len(result.notes))]
        kept = tuple(
            note for note, verdict in zip(result.notes, judged, strict=True) if verdict is Verdict.ALLOW
        )
        deck_allowed = verdicts.deck is Verdict.ALLOW
        logger.log(
            logging.INFO if len(kept) == len(result.notes) and deck_allowed else logging.WARNING,
            "content_screened",
            extra={
                "job_id": str(job_id),
                "reply_stop": reply.stop,
                "screened_notes": len(result.notes),
                "kept_notes": len(kept),
                "blocked_notes": judged.count(Verdict.BLOCK),
                "unjudged_notes": judged.count(None),
                "deck_replaced": not deck_allowed,
            },
        )
        deck = result.deck if deck_allowed else repair_deck(NO_DECK_REPLACEMENT, request.topic)
        return GenerationResult(deck=deck, notes=kept)
