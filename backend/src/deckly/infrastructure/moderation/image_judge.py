import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from deckly.domain.deck import Deck
from deckly.infrastructure.card_generator.untrusted import as_object
from deckly.infrastructure.llm.client import LlmClient, LlmReply, LlmStop
from deckly.infrastructure.media.judging import OMITTED, Omitted, Shortlist, Verdict
from deckly.infrastructure.moderation.moderator import (
    UnusableVerdictError,
    moderation_faults_as_upstream,
    reply_object,
)
from deckly.infrastructure.moderation.prompt import FIRST_CANDIDATE_NUMBER, build_image_prompt, note_number

logger = logging.getLogger(__name__)

NO_RANKINGS: Mapping[str, object] = MappingProxyType({})


def image_rankings(reply: LlmReply) -> Mapping[str, object]:
    if reply.stop is LlmStop.REFUSED:
        return NO_RANKINGS
    notes = as_object(reply_object(reply).get("notes"))
    if notes is None:
        message = "image screening reply carries no note rankings"
        raise UnusableVerdictError(message)
    return notes


def candidate_position(value: object, count: int) -> int | None:
    match value:
        case bool():
            return None
        case int():
            number = value
        case str() if value.strip().isascii() and value.strip().isdigit():
            try:
                number = int(value)
            except ValueError:
                return None
        case _:
            return None
    position = number - FIRST_CANDIDATE_NUMBER
    return position if 0 <= position < count else None


def verdict_of(shortlist: Shortlist, value: object) -> Verdict:
    if not isinstance(value, list):
        return OMITTED
    count = len(shortlist.images)
    positions = (candidate_position(item, count) for item in value)
    unique = dict.fromkeys(position for position in positions if position is not None)
    return tuple(shortlist.images[position] for position in unique)


def verdicts_of(
    shortlists: tuple[Shortlist, ...], rankings: Mapping[str, object], *, refused: bool
) -> tuple[Verdict, ...]:
    if refused:
        return tuple(() for _ in shortlists)
    return tuple(
        verdict_of(shortlist, rankings.get(note_number(index))) for index, shortlist in enumerate(shortlists)
    )


@dataclass(frozen=True, slots=True)
class LlmCandidateJudge:
    llm: LlmClient

    async def rank(self, deck: Deck, shortlists: tuple[Shortlist, ...]) -> tuple[Verdict, ...]:
        with moderation_faults_as_upstream({"subject": "images"}):
            reply = await self.llm.complete(build_image_prompt(deck, shortlists))
            rankings = image_rankings(reply)
        ranked = verdicts_of(shortlists, rankings, refused=reply.stop is LlmStop.REFUSED)
        explicit = tuple(verdict for verdict in ranked if not isinstance(verdict, Omitted))
        candidates = sum(len(shortlist.images) for shortlist in shortlists)
        accepted = sum(len(ranking) for ranking in explicit)
        logger.log(
            logging.WARNING if reply.stop is LlmStop.REFUSED else logging.INFO,
            "images_screened",
            extra={
                "reply_stop": reply.stop,
                "screened_notes": len(shortlists),
                "screened_candidates": candidates,
                "accepted_candidates": accepted,
                "notes_without_image": len(ranked) - sum(1 for ranking in explicit if ranking),
                "omitted_notes": len(ranked) - len(explicit),
            },
        )
        return ranked
