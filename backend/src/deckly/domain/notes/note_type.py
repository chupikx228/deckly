from enum import StrEnum


class NoteType(StrEnum):
    BASIC = "basic"
    BASIC_REVERSED = "basic_reversed"
    BASIC_OPTIONAL_REVERSED = "basic_optional_reversed"
    BASIC_TYPE_IN = "basic_type_in"
    CLOZE = "cloze"
    MULTIPLE_CHOICE = "multiple_choice"
    IMAGE_OCCLUSION = "image_occlusion"
