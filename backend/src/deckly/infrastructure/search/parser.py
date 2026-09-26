import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from deckly.application.ports import RetrievedPage, SourceMaterial
from deckly.domain.exceptions import InvalidSourceError
from deckly.domain.generation import GenerationRequest
from deckly.domain.text import visible_length
from deckly.infrastructure.search.cleaning import clean_text, clean_title, garbage_share

logger = logging.getLogger(__name__)

RAW_WINDOW_MULTIPLIER = 4
MAX_GARBAGE_SHARE = 0.1
MIN_VISIBLE_CHARACTERS = 50


class DropReason(StrEnum):
    UNREADABLE = "unreadable"
    TOO_SHORT = "too_short"
    INVALID_SOURCE = "invalid_source"
    DUPLICATE = "duplicate"


class DroppedPageError(Exception):
    def __init__(self, reason: DropReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CleaningSourceParser:
    max_characters: int

    async def parse(
        self, job_id: UUID, request: GenerationRequest, pages: tuple[RetrievedPage, ...]
    ) -> tuple[SourceMaterial, ...]:
        del request
        return await asyncio.to_thread(self._parse, job_id, pages)

    def _parse(self, job_id: UUID, pages: tuple[RetrievedPage, ...]) -> tuple[SourceMaterial, ...]:
        drops: Counter[DropReason] = Counter()
        material: list[SourceMaterial] = []
        seen: set[str] = set()
        for page in pages:
            try:
                entry = self._material(page)
            except DroppedPageError as error:
                drops[error.reason] += 1
                continue
            if entry.text in seen:
                drops[DropReason.DUPLICATE] += 1
                continue
            seen.add(entry.text)
            material.append(entry)
        logger.log(
            logging.INFO if material else logging.WARNING,
            "sources_parsed",
            extra={
                "job_id": str(job_id),
                "pages": len(pages),
                "kept_pages": len(material),
                "dropped_pages": dict(drops),
            },
        )
        return tuple(material)

    def _material(self, page: RetrievedPage) -> SourceMaterial:
        raw = page.content[: self.max_characters * RAW_WINDOW_MULTIPLIER]
        if garbage_share(raw) > MAX_GARBAGE_SHARE:
            raise DroppedPageError(DropReason.UNREADABLE)
        text = clean_text(raw, self.max_characters)
        if visible_length(text) < MIN_VISIBLE_CHARACTERS:
            raise DroppedPageError(DropReason.TOO_SHORT)
        try:
            source = replace(page.source, title=clean_title(page.source.title))
        except InvalidSourceError as error:
            raise DroppedPageError(DropReason.INVALID_SOURCE) from error
        return SourceMaterial(source=source, text=text)
