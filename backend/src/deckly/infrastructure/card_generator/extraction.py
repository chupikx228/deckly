import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

DOCUMENT_START = re.compile(r'\{\s*"(?:deck|notes)"\s*:')
NOTES_ARRAY = re.compile(r'"notes"\s*:\s*\[')
DECK_OBJECT = re.compile(r'"deck"\s*:\s*\{')
NOTE_START = re.compile(r'\{\s*"noteType"\s*:')
OBJECT_OPENING = "{"
ARRAY_OPENING = "["
ARRAY_CLOSING = "]"
DOCUMENT_KEYS = frozenset({"deck", "notes"})
SEPARATORS = frozenset({",", " ", "\t", "\n", "\r"})
DECODER = json.JSONDecoder(strict=False)


@dataclass(frozen=True, slots=True)
class ModelDocument:
    deck: object
    notes: tuple[object, ...]
    complete: bool


EMPTY_DOCUMENT = ModelDocument(deck=None, notes=(), complete=False)


@dataclass(frozen=True, slots=True)
class Decoded:
    value: object
    end: int


@dataclass(frozen=True, slots=True)
class Undecodable:
    resume_at: int


@dataclass(frozen=True, slots=True)
class LocatedDocument:
    document: ModelDocument
    end: int


def decode_at(text: str, index: int) -> Decoded | Undecodable:
    try:
        value, end = DECODER.raw_decode(text, index)
    except json.JSONDecodeError as error:
        return Undecodable(resume_at=max(error.pos, index + 1))
    except ValueError:
        return Undecodable(resume_at=index + 1)
    except RecursionError:
        return Undecodable(resume_at=len(text))
    return Decoded(value=value, end=end)


def first_opening(text: str) -> int:
    openings = [
        position for position in (text.find(OBJECT_OPENING), text.find(ARRAY_OPENING)) if position >= 0
    ]
    return min(openings, default=-1)


def complete_document(value: object) -> ModelDocument | None:
    if isinstance(value, list):
        return ModelDocument(deck=None, notes=tuple(value), complete=True)
    if isinstance(value, dict) and not DOCUMENT_KEYS.isdisjoint(value):
        notes = value.get("notes")
        return ModelDocument(
            deck=value.get("deck"), notes=tuple(notes) if isinstance(notes, list) else (), complete=True
        )
    return None


def document_at(text: str, start: int) -> LocatedDocument | None:
    decoded = decode_at(text, start)
    if not isinstance(decoded, Decoded):
        return None
    document = complete_document(decoded.value)
    return None if document is None else LocatedDocument(document=document, end=decoded.end)


def skip_separators(text: str, index: int) -> int:
    while index < len(text) and text[index] in SEPARATORS:
        index += 1
    return index


def salvage_deck(text: str, start: int) -> object:
    match = DECK_OBJECT.search(text, start)
    if match is None:
        return None
    decoded = decode_at(text, match.end() - 1)
    return decoded.value if isinstance(decoded, Decoded) else None


def salvage_notes(text: str, start: int) -> tuple[object, ...]:
    match = NOTES_ARRAY.search(text, start)
    if match is None:
        return ()
    notes: list[object] = []
    index = match.end()
    while (index := skip_separators(text, index)) < len(text) and text[index] != ARRAY_CLOSING:
        decoded = decode_at(text, index)
        if isinstance(decoded, Decoded):
            notes.append(decoded.value)
            index = decoded.end
            continue
        resumed = NOTE_START.search(text, decoded.resume_at)
        if resumed is None:
            break
        index = resumed.start()
    return tuple(notes)


def salvaged_document(text: str, start: int) -> ModelDocument:
    return ModelDocument(deck=salvage_deck(text, start), notes=salvage_notes(text, start), complete=False)


def top_level_documents(text: str) -> list[ModelDocument]:
    documents: list[ModelDocument] = []
    index = 0
    while (match := DOCUMENT_START.search(text, index)) is not None:
        located = document_at(text, match.start())
        if located is None:
            documents.append(salvaged_document(text, match.start()))
            break
        documents.append(located.document)
        index = located.end
    return documents


def merged_document(documents: Sequence[ModelDocument]) -> ModelDocument:
    decks = (document.deck for document in documents if document.deck is not None)
    return ModelDocument(
        deck=next(decks, None),
        notes=tuple(note for document in documents for note in document.notes),
        complete=all(document.complete for document in documents),
    )


def extract_document(text: str) -> ModelDocument:
    documents = top_level_documents(text)
    if documents:
        return merged_document(documents)
    start = first_opening(text)
    if start < 0:
        return EMPTY_DOCUMENT
    located = document_at(text, start)
    return salvaged_document(text, start) if located is None else located.document
