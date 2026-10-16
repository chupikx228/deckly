import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote
from xml.parsers import expat

from deckly.application.exceptions import UnreadableDocumentError

NAMESPACE_SEPARATOR = " "
FINAL_CHUNK = True
PART_SEPARATOR = "/"
PARENT_SEGMENT = ".."
RELS_DIRECTORY = "_rels"
RELS_SUFFIX = ".rels"
EXTERNAL_TARGET_MODE = "External"
PACKAGE_RELATIONSHIP_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/package/2006/relationships",
        "http://purl.oclc.org/ooxml/package/relationships",
    }
)
RELATIONSHIP_ELEMENT = "Relationship"

type ElementStart = Callable[[str, str, Mapping[str, str]], None]
type ElementEnd = Callable[[str, str], None]
type CharacterData = Callable[[str], None]


class UnsafeXmlError(UnreadableDocumentError):
    pass


def split_name(qualified: str) -> tuple[str, str]:
    namespace, _, local = qualified.rpartition(NAMESPACE_SEPARATOR)
    return namespace, local


def forbid_declaration(*_: object) -> None:
    message = "document XML must not declare a DOCTYPE or entities"
    raise UnsafeXmlError(message)


def parse_xml(content: bytes, start: ElementStart, end: ElementEnd, text: CharacterData) -> None:
    parser = expat.ParserCreate(namespace_separator=NAMESPACE_SEPARATOR)
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = forbid_declaration
    parser.EntityDeclHandler = forbid_declaration
    parser.StartElementHandler = lambda name, attributes: start(*split_name(name), attributes)
    parser.EndElementHandler = lambda name: end(*split_name(name))
    parser.CharacterDataHandler = text
    try:
        parser.Parse(content, FINAL_CHUNK)
    except expat.ExpatError as error:
        message = f"document XML is malformed: {expat.errors.messages[error.code]}"
        raise UnreadableDocumentError(message) from error


@dataclass(frozen=True, slots=True)
class Relationship:
    relationship_type: str
    target: str


def relationships_part(part: str) -> str:
    directory, name = posixpath.split(part)
    return posixpath.join(directory, RELS_DIRECTORY, name + RELS_SUFFIX)


def resolve_target(part: str, target: str) -> str | None:
    decoded = unquote(target)
    if decoded.startswith(PART_SEPARATOR):
        joined = decoded.lstrip(PART_SEPARATOR)
    else:
        joined = posixpath.join(posixpath.dirname(part), decoded)
    resolved = posixpath.normpath(joined)
    if resolved == PARENT_SEGMENT or resolved.startswith(PARENT_SEGMENT + PART_SEPARATOR):
        return None
    return resolved


@dataclass
class RelationshipCollector:
    part: str
    found: dict[str, Relationship] = field(default_factory=dict)

    def start(self, namespace: str, local: str, attributes: Mapping[str, str]) -> None:
        if namespace not in PACKAGE_RELATIONSHIP_NAMESPACES or local != RELATIONSHIP_ELEMENT:
            return
        if attributes.get("TargetMode") == EXTERNAL_TARGET_MODE:
            return
        identifier, target = attributes.get("Id"), attributes.get("Target")
        if identifier is None or target is None:
            return
        resolved = resolve_target(self.part, target)
        if resolved is not None:
            self.found[identifier] = Relationship(attributes.get("Type", ""), resolved)

    def end(self, namespace: str, local: str) -> None:
        del namespace, local

    def text(self, data: str) -> None:
        del data


def parse_relationships(part: str, content: bytes) -> Mapping[str, Relationship]:
    collector = RelationshipCollector(part)
    parse_xml(content, collector.start, collector.end, collector.text)
    return collector.found
