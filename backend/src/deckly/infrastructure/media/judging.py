from dataclasses import dataclass
from typing import Protocol

from deckly.domain.deck import Deck
from deckly.domain.media import Media
from deckly.domain.notes.note import Note
from deckly.infrastructure.media.client import ImageCandidate


@dataclass(frozen=True, slots=True)
class ShortlistedImage:
    candidate: ImageCandidate
    media: Media


@dataclass(frozen=True, slots=True)
class Shortlist:
    note: Note
    picture: str
    images: tuple[ShortlistedImage, ...]


type Ranking = tuple[ShortlistedImage, ...]


class CandidateJudge(Protocol):
    async def rank(self, deck: Deck, shortlists: tuple[Shortlist, ...]) -> tuple[Ranking, ...]: ...
