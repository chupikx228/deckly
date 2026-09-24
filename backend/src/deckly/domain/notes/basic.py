from dataclasses import dataclass
from typing import ClassVar

from deckly.domain.notes.fields import NoteFields, require_bool, require_text
from deckly.domain.notes.note_type import NoteType


@dataclass(frozen=True, slots=True)
class FrontBackFields(NoteFields):
    front: str
    back: str

    def __post_init__(self) -> None:
        require_text(self.front, "front")
        require_text(self.back, "back")


@dataclass(frozen=True, slots=True)
class BasicFields(FrontBackFields):
    note_type: ClassVar[NoteType] = NoteType.BASIC


@dataclass(frozen=True, slots=True)
class BasicReversedFields(FrontBackFields):
    note_type: ClassVar[NoteType] = NoteType.BASIC_REVERSED


@dataclass(frozen=True, slots=True)
class BasicTypeInFields(FrontBackFields):
    note_type: ClassVar[NoteType] = NoteType.BASIC_TYPE_IN


@dataclass(frozen=True, slots=True)
class BasicOptionalReversedFields(FrontBackFields):
    note_type: ClassVar[NoteType] = NoteType.BASIC_OPTIONAL_REVERSED

    add_reverse: bool

    def __post_init__(self) -> None:
        FrontBackFields.__post_init__(self)
        require_bool(self.add_reverse, "addReverse")
