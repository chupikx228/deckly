from collections.abc import Mapping
from types import MappingProxyType

from deckly.domain.exceptions import UnsupportedNoteTypeError
from deckly.domain.notes.basic import (
    BasicFields,
    BasicOptionalReversedFields,
    BasicReversedFields,
    BasicTypeInFields,
)
from deckly.domain.notes.cloze import ClozeFields
from deckly.domain.notes.fields import NoteFields
from deckly.domain.notes.image_occlusion import ImageOcclusionFields
from deckly.domain.notes.multiple_choice import MultipleChoiceFields
from deckly.domain.notes.note_type import NoteType

NOTE_FIELDS_BY_TYPE: Mapping[NoteType, type[NoteFields]] = MappingProxyType(
    {
        fields_type.note_type: fields_type
        for fields_type in (
            BasicFields,
            BasicReversedFields,
            BasicOptionalReversedFields,
            BasicTypeInFields,
            ClozeFields,
            MultipleChoiceFields,
            ImageOcclusionFields,
        )
    }
)


def fields_type_for(note_type: NoteType) -> type[NoteFields]:
    fields_type = NOTE_FIELDS_BY_TYPE.get(note_type)
    if fields_type is None:
        message = f"note type {note_type} has no field shape"
        raise UnsupportedNoteTypeError(message)
    return fields_type
