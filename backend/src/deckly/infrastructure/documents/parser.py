import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Generator, Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from deckly.application.exceptions import DocumentHasNoTextError
from deckly.application.ports import SourceMaterial
from deckly.domain.document import Document, DocumentFormat
from deckly.domain.source import CitationUnit, DocumentSource
from deckly.domain.text import is_blank, visible_length
from deckly.infrastructure.documents.extraction import ExtractedUnit, ExtractionDeadline, MonotonicClock
from deckly.infrastructure.documents.pdf import pdf_pages
from deckly.infrastructure.documents.pptx import pptx_slides
from deckly.infrastructure.search.cleaning import clean_text, clean_title, garbage_share
from deckly.infrastructure.search.parser import (
    MAX_GARBAGE_SHARE,
    MIN_VISIBLE_CHARACTERS,
    RAW_WINDOW_MULTIPLIER,
)

logger = logging.getLogger(__name__)

type UnitExtractor = Callable[[bytes, ExtractionDeadline], Generator[ExtractedUnit]]


@dataclass(frozen=True, slots=True)
class FormatReader:
    extract: UnitExtractor
    unit: CitationUnit


FORMAT_READERS: Mapping[DocumentFormat, FormatReader] = MappingProxyType(
    {
        DocumentFormat.PDF: FormatReader(extract=pdf_pages, unit=CitationUnit.PAGE),
        DocumentFormat.PPTX: FormatReader(extract=pptx_slides, unit=CitationUnit.SLIDE),
    }
)


class UnitOutcome(StrEnum):
    KEPT = "kept"
    BLANK = "blank"
    GARBLED = "garbled"


@dataclass(frozen=True, slots=True)
class DocumentTextParser:
    max_characters: int
    deadline_seconds: float
    clock: MonotonicClock = time.monotonic

    async def parse(self, document: Document, content: bytes) -> tuple[SourceMaterial, ...]:
        return await asyncio.to_thread(self._parse, document, content)

    def _parse(self, document: Document, content: bytes) -> tuple[SourceMaterial, ...]:
        reader = FORMAT_READERS[document.document_format]
        deadline = ExtractionDeadline.after(self.deadline_seconds, self.clock)
        title = clean_title(document.filename)
        outcomes: Counter[UnitOutcome] = Counter()
        material: list[SourceMaterial] = []
        remaining = self.max_characters
        with closing(reader.extract(content, deadline)) as units:
            for extracted in units:
                window = extracted.text[: remaining * RAW_WINDOW_MULTIPLIER]
                text = self._text(window, remaining)
                outcome = UnitOutcome.KEPT if text else self._rejection(window)
                outcomes[outcome] += 1
                if text:
                    source = DocumentSource(
                        title=title,
                        document_id=document.document_id,
                        unit=reader.unit,
                        number=extracted.number,
                    )
                    material.append(SourceMaterial(source=source, text=text))
                    remaining -= len(text)
                if remaining < MIN_VISIBLE_CHARACTERS:
                    break
        visible = sum(visible_length(entry.text) for entry in material)
        logger.log(
            logging.INFO if visible >= MIN_VISIBLE_CHARACTERS else logging.WARNING,
            "document_parsed",
            extra={
                "document_id": str(document.document_id),
                "content_type": document.document_format,
                "units": sum(outcomes.values()),
                "unit_outcomes": dict(outcomes),
                "characters": self.max_characters - remaining,
                "budget_exhausted": remaining < MIN_VISIBLE_CHARACTERS,
            },
        )
        if visible < MIN_VISIBLE_CHARACTERS:
            message = f"the document has {visible} visible characters of extractable text"
            raise DocumentHasNoTextError(message)
        return tuple(material)

    @staticmethod
    def _text(window: str, limit: int) -> str:
        if garbage_share(window) > MAX_GARBAGE_SHARE:
            return ""
        text = clean_text(window, limit)
        return "" if is_blank(text) else text

    @staticmethod
    def _rejection(raw: str) -> UnitOutcome:
        return UnitOutcome.GARBLED if garbage_share(raw) > MAX_GARBAGE_SHARE else UnitOutcome.BLANK
