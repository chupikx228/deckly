import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from deckly.domain.deck import Deck
from deckly.infrastructure.card_generator.untrusted import as_object
from deckly.infrastructure.llm.client import LlmClient, LlmReply, LlmStop
from deckly.infrastructure.media.judging import Ranking, Shortlist
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


def ranking_of(shortlist: Shortlist, value: object) -> Ranking:
    if not isinstance(value, list):
        return ()
    count = len(shortlist.images)
    positions = (candidate_position(item, count) for item in value)
    unique = dict.fromkeys(position for position in positions if position is not None)
    return tuple(shortlist.images[position] for position in unique)


@dataclass(frozen=True, slots=True)
class LlmCandidateJudge:
    llm: LlmClient

    async def rank(self, deck: Deck, shortlists: tuple[Shortlist, ...]) -> tuple[Ranking, ...]:
        with moderation_faults_as_upstream({"subject": "images"}):
            reply = await self.llm.complete(build_image_prompt(deck, shortlists))
            rankings = image_rankings(reply)
        ranked = tuple(
            ranking_of(shortlist, rankings.get(note_number(index)))
            for index, shortlist in enumerate(shortlists)
        )
        candidates = sum(len(shortlist.images) for shortlist in shortlists)
        accepted = sum(len(ranking) for ranking in ranked)
        logger.log(
            logging.WARNING if reply.stop is LlmStop.REFUSED else logging.INFO,
            "images_screened",
            extra={
                "reply_stop": reply.stop,
                "screened_notes": len(shortlists),
                "screened_candidates": candidates,
                "accepted_candidates": accepted,
                "notes_without_image": sum(1 for ranking in ranked if not ranking),
            },
        )
        return ranked
