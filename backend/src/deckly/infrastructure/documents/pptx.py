from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from itertools import islice

from deckly.application.exceptions import UnreadableDocumentError
from deckly.infrastructure.documents.archive import BoundedArchive
from deckly.infrastructure.documents.extraction import MAX_EXTRACTED_UNITS, ExtractedUnit, ExtractionDeadline
from deckly.infrastructure.documents.ooxml import (
    NAMESPACE_SEPARATOR,
    Relationship,
    parse_relationships,
    parse_xml,
    relationships_part,
)
from deckly.infrastructure.documents.sniffing import UNREADABLE_ARCHIVE_ERRORS

PRESENTATION_PART = "ppt/presentation.xml"
DRAWING_NAMESPACES = frozenset(
    {"http://schemas.openxmlformats.org/drawingml/2006/main", "http://purl.oclc.org/ooxml/drawingml/main"}
)
PRESENTATION_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/presentationml/2006/main",
        "http://purl.oclc.org/ooxml/presentationml/main",
    }
)
RELATIONSHIP_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
MARKUP_COMPATIBILITY_NAMESPACE = "http://schemas.openxmlformats.org/markup-compatibility/2006"
RELATIONSHIP_ID_ATTRIBUTES = tuple(
    f"{namespace}{NAMESPACE_SEPARATOR}id" for namespace in RELATIONSHIP_NAMESPACES
)
SLIDE_RELATIONSHIPS = frozenset(f"{namespace}/slide" for namespace in RELATIONSHIP_NAMESPACES)
NOTES_RELATIONSHIPS = frozenset(f"{namespace}/notesSlide" for namespace in RELATIONSHIP_NAMESPACES)
SLIDE_ID_ELEMENT = "sldId"
SHAPE_ELEMENT = "sp"
PLACEHOLDER_ELEMENT = "ph"
TEXT_ELEMENT = "t"
PARAGRAPH_ELEMENT = "p"
BREAK_ELEMENT = "br"
FIELD_ELEMENT = "fld"
FALLBACK_ELEMENT = "Fallback"
PLACEHOLDER_TYPE_ATTRIBUTE = "type"
UNTYPED_PLACEHOLDER = "obj"
NOTES_BODY_PLACEHOLDER = "body"
BOILERPLATE_PLACEHOLDERS = frozenset({"sldNum", "dt", "ftr", "hdr"})
LINE_BREAK = "\n"


@dataclass
class Shape:
    placeholder: str | None = None
    lines: list[str] = field(default_factory=list)


@dataclass
class SlideIdCollector:
    relationship_ids: list[str] = field(default_factory=list)

    def start(self, namespace: str, local: str, attributes: Mapping[str, str]) -> None:
        if namespace not in PRESENTATION_NAMESPACES or local != SLIDE_ID_ELEMENT:
            return
        identifier = next((attributes[key] for key in RELATIONSHIP_ID_ATTRIBUTES if key in attributes), None)
        if identifier is None:
            message = "a slide in the presentation has no relationship id"
            raise UnreadableDocumentError(message)
        self.relationship_ids.append(identifier)

    def end(self, namespace: str, local: str) -> None:
        del namespace, local

    def text(self, data: str) -> None:
        del data


@dataclass
class TextCollector:
    notes: bool
    loose: Shape = field(default_factory=Shape)
    shapes: list[Shape] = field(default_factory=list)
    text_depth: int = 0
    field_depth: int = 0
    fallback_depth: int = 0

    @property
    def current(self) -> Shape:
        return self.shapes[-1] if self.shapes else self.loose

    def start(self, namespace: str, local: str, attributes: Mapping[str, str]) -> None:
        if namespace == MARKUP_COMPATIBILITY_NAMESPACE and local == FALLBACK_ELEMENT:
            self.fallback_depth += 1
        elif namespace in PRESENTATION_NAMESPACES and local == SHAPE_ELEMENT:
            self.shapes.append(Shape())
        elif namespace in PRESENTATION_NAMESPACES and local == PLACEHOLDER_ELEMENT:
            self.current.placeholder = attributes.get(PLACEHOLDER_TYPE_ATTRIBUTE, UNTYPED_PLACEHOLDER)
        elif namespace in DRAWING_NAMESPACES and local == TEXT_ELEMENT:
            self.text_depth += 1
        elif namespace in DRAWING_NAMESPACES and local == FIELD_ELEMENT:
            self.field_depth += 1
        elif namespace in DRAWING_NAMESPACES and local == BREAK_ELEMENT:
            self._write(LINE_BREAK)

    def end(self, namespace: str, local: str) -> None:
        if namespace == MARKUP_COMPATIBILITY_NAMESPACE and local == FALLBACK_ELEMENT:
            self.fallback_depth -= 1
        elif namespace in PRESENTATION_NAMESPACES and local == SHAPE_ELEMENT and self.shapes:
            shape = self.shapes.pop()
            if self._keeps(shape):
                self.current.lines.extend((*shape.lines, LINE_BREAK))
        elif namespace in DRAWING_NAMESPACES and local == TEXT_ELEMENT:
            self.text_depth -= 1
        elif namespace in DRAWING_NAMESPACES and local == FIELD_ELEMENT:
            self.field_depth -= 1
        elif namespace in DRAWING_NAMESPACES and local == PARAGRAPH_ELEMENT:
            self._write(LINE_BREAK)

    def text(self, data: str) -> None:
        if self.text_depth > 0 and self.field_depth == 0:
            self._write(data)

    def collected(self) -> str:
        return "".join(self.loose.lines)

    def _write(self, data: str) -> None:
        if self.fallback_depth == 0 and (self.shapes or not self.notes):
            self.current.lines.append(data)

    def _keeps(self, shape: Shape) -> bool:
        if self.notes:
            return shape.placeholder == NOTES_BODY_PLACEHOLDER
        return shape.placeholder not in BOILERPLATE_PLACEHOLDERS


def part_text(archive: BoundedArchive, part: str, *, notes: bool) -> str:
    collector = TextCollector(notes=notes)
    parse_xml(archive.read(part), collector.start, collector.end, collector.text)
    return collector.collected()


def part_relationships(archive: BoundedArchive, part: str) -> Mapping[str, Relationship]:
    rels = relationships_part(part)
    return parse_relationships(part, archive.read(rels)) if archive.has(rels) else {}


def slide_parts(archive: BoundedArchive) -> list[str]:
    collector = SlideIdCollector()
    parse_xml(archive.read(PRESENTATION_PART), collector.start, collector.end, collector.text)
    relationships = part_relationships(archive, PRESENTATION_PART)
    parts: list[str] = []
    for identifier in collector.relationship_ids:
        relationship = relationships.get(identifier)
        if relationship is None or relationship.relationship_type not in SLIDE_RELATIONSHIPS:
            message = f"slide relationship {identifier!r} does not point at a slide"
            raise UnreadableDocumentError(message)
        parts.append(relationship.target)
    return parts


def notes_part(archive: BoundedArchive, slide: str) -> str | None:
    return next(
        (
            relationship.target
            for relationship in part_relationships(archive, slide).values()
            if relationship.relationship_type in NOTES_RELATIONSHIPS
        ),
        None,
    )


def slide_text(archive: BoundedArchive, slide: str) -> str:
    text = part_text(archive, slide, notes=False)
    notes = notes_part(archive, slide)
    if notes is None:
        return text
    return text + LINE_BREAK + part_text(archive, notes, notes=True)


def open_archive(content: bytes) -> BoundedArchive:
    try:
        return BoundedArchive.of(content)
    except UNREADABLE_ARCHIVE_ERRORS as error:
        message = f"the presentation is not a readable archive: {type(error).__name__}"
        raise UnreadableDocumentError(message) from error


def pptx_slides(content: bytes, deadline: ExtractionDeadline) -> Generator[ExtractedUnit]:
    with open_archive(content) as archive:
        try:
            slides = slide_parts(archive)
            for number, slide in enumerate(islice(slides, MAX_EXTRACTED_UNITS), start=1):
                deadline.check()
                yield ExtractedUnit(number=number, text=slide_text(archive, slide))
        except UNREADABLE_ARCHIVE_ERRORS as error:
            message = f"the presentation could not be read: {type(error).__name__}"
            raise UnreadableDocumentError(message) from error
