import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from deckly.domain.notes.basic import (
    BasicFields,
    BasicOptionalReversedFields,
    BasicReversedFields,
    BasicTypeInFields,
)
from deckly.domain.notes.cloze import ClozeFields
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.multiple_choice import MAX_DISTRACTORS, MIN_DISTRACTORS, MultipleChoiceFields
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.card_generator.untrusted import (
    JsonObject,
    read_flag,
    read_optional_text,
    read_text,
    read_texts,
    require_keys,
)

FRONT_BACK_KEYS = frozenset({"front", "back"})
OPTIONAL_REVERSED_KEYS = frozenset({"front", "back", "addReverse"})
CLOZE_KEYS = frozenset({"text"})
CLOZE_OPTIONAL_KEYS = frozenset({"extra"})
MULTIPLE_CHOICE_KEYS = frozenset({"question", "answer", "distractors"})
LENIENT_CLOZE_OPENING = re.compile(r"\{\{[cC]([0-9]+)::")
INSIGNIFICANT_DIGIT = "0"

BASIC_INSTRUCTIONS = (
    '"basic": a question or cue on the front and its answer on the back.\n'
    '  {"noteType": "basic", "fields": {"front": "<question>", "back": "<answer>"}, "sources": [1]}'
)
BASIC_REVERSED_INSTRUCTIONS = (
    '"basic_reversed": two sides that each work as a question for the other, such as a term and its '
    "definition. The app also asks from back to front.\n"
    '  {"noteType": "basic_reversed", "fields": {"front": "<term>", "back": "<definition>"}, "sources": [1]}'
)
BASIC_TYPE_IN_INSTRUCTIONS = (
    '"basic_type_in": the learner types the answer, so the back is short and exact: a word, a number '
    "or a short phrase.\n"
    '  {"noteType": "basic_type_in", "fields": {"front": "<question>", "back": "<short exact answer>"}, '
    '"sources": [1]}'
)
OPTIONAL_REVERSED_INSTRUCTIONS = (
    '"basic_optional_reversed": like basic, plus addReverse, a JSON boolean that is true when the '
    "back-to-front card is also worth studying and false otherwise.\n"
    '  {"noteType": "basic_optional_reversed", "fields": {"front": "<question>", "back": "<answer>", '
    '"addReverse": true}, "sources": [1]}'
)
CLOZE_INSTRUCTIONS = (
    '"cloze": a sentence in text with each hidden part written as {{c1::hidden part}}, numbered c1, c2, '
    "c3 and so on without gaps. Parts that share a number are hidden together. extra is optional "
    "context shown with the answer.\n"
    '  {"noteType": "cloze", "fields": {"text": "<sentence with {{c1::a hidden part}}>", '
    '"extra": "<context>"}, "sources": [1]}'
)
MULTIPLE_CHOICE_INSTRUCTIONS = (
    f'"multiple_choice": a question, its correct answer and {MIN_DISTRACTORS} to {MAX_DISTRACTORS} '
    "distractors that are plausible, wrong, different from each other and never the correct answer.\n"
    '  {"noteType": "multiple_choice", "fields": {"question": "<question>", "answer": "<correct answer>", '
    '"distractors": ["<wrong answer>", "<wrong answer>"]}, "sources": [1]}'
)


class NoteTypeHandler(Protocol):
    @property
    def note_type(self) -> NoteType: ...

    @property
    def instructions(self) -> str: ...

    def parse(self, fields: JsonObject) -> NoteFields: ...


def cloze_rank(digits: str) -> tuple[int, str]:
    significant = digits.lstrip(INSIGNIFICANT_DIGIT) or INSIGNIFICANT_DIGIT
    return len(significant), significant


def renumber_cloze(text: str) -> str:
    ranks = sorted({cloze_rank(digits) for digits in LENIENT_CLOZE_OPENING.findall(text)})
    numbers = {rank: number for number, rank in enumerate(ranks, start=1)}
    return LENIENT_CLOZE_OPENING.sub(lambda match: f"{{{{c{numbers[cloze_rank(match.group(1))]}::", text)


@dataclass(frozen=True, slots=True)
class FrontBackHandler:
    shape: type[BasicFields] | type[BasicReversedFields] | type[BasicTypeInFields]
    instructions: str

    @property
    def note_type(self) -> NoteType:
        return self.shape.note_type

    def parse(self, fields: JsonObject) -> NoteFields:
        require_keys(fields, FRONT_BACK_KEYS)
        return self.shape(front=read_text(fields, "front"), back=read_text(fields, "back"))


@dataclass(frozen=True, slots=True)
class OptionalReversedHandler:
    instructions: str = OPTIONAL_REVERSED_INSTRUCTIONS

    @property
    def note_type(self) -> NoteType:
        return BasicOptionalReversedFields.note_type

    def parse(self, fields: JsonObject) -> NoteFields:
        require_keys(fields, OPTIONAL_REVERSED_KEYS)
        return BasicOptionalReversedFields(
            front=read_text(fields, "front"),
            back=read_text(fields, "back"),
            add_reverse=read_flag(fields, "addReverse"),
        )


@dataclass(frozen=True, slots=True)
class ClozeHandler:
    instructions: str = CLOZE_INSTRUCTIONS

    @property
    def note_type(self) -> NoteType:
        return ClozeFields.note_type

    def parse(self, fields: JsonObject) -> NoteFields:
        require_keys(fields, CLOZE_KEYS, CLOZE_OPTIONAL_KEYS)
        return ClozeFields(
            text=renumber_cloze(read_text(fields, "text")), extra=read_optional_text(fields, "extra")
        )


@dataclass(frozen=True, slots=True)
class MultipleChoiceHandler:
    instructions: str = MULTIPLE_CHOICE_INSTRUCTIONS

    @property
    def note_type(self) -> NoteType:
        return MultipleChoiceFields.note_type

    def parse(self, fields: JsonObject) -> NoteFields:
        require_keys(fields, MULTIPLE_CHOICE_KEYS)
        return MultipleChoiceFields(
            question=read_text(fields, "question"),
            answer=read_text(fields, "answer"),
            distractors=read_texts(fields, "distractors"),
        )


HANDLERS: tuple[NoteTypeHandler, ...] = (
    FrontBackHandler(BasicFields, BASIC_INSTRUCTIONS),
    FrontBackHandler(BasicReversedFields, BASIC_REVERSED_INSTRUCTIONS),
    FrontBackHandler(BasicTypeInFields, BASIC_TYPE_IN_INSTRUCTIONS),
    OptionalReversedHandler(),
    ClozeHandler(),
    MultipleChoiceHandler(),
)

NOTE_TYPE_HANDLERS: Mapping[NoteType, NoteTypeHandler] = MappingProxyType(
    {handler.note_type: handler for handler in HANDLERS}
)
