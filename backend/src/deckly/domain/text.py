import unicodedata
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

WEB_URL_SCHEMES = frozenset({"http", "https"})
NUL = "\N{NULL}"
SURROGATE_CATEGORY = "Cs"
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf"})
MARK_CATEGORIES = frozenset({"Mn", "Mc", "Me"})
BLANK_LOOKING_CHARACTERS = frozenset(
    {
        "\N{HANGUL CHOSEONG FILLER}",
        "\N{HANGUL JUNGSEONG FILLER}",
        "\N{HANGUL FILLER}",
        "\N{HALFWIDTH HANGUL FILLER}",
        "\N{BRAILLE PATTERN BLANK}",
        "\N{MUSICAL SYMBOL NULL NOTEHEAD}",
    }
)
DEFAULT_IGNORABLE_RANGES = (
    range(0x034F, 0x0350),
    range(0x17B4, 0x17B6),
    range(0x180B, 0x1810),
    range(0x2065, 0x2066),
    range(0xFE00, 0xFE10),
    range(0xFFF0, 0xFFF9),
    range(0xE0000, 0xE1000),
)
EMOJI_TAG_CHARACTERS = range(0xE0020, 0xE0080)
UUID_VERSION = 4
UTF16_CODE_UNIT_BYTES = 2
COMPARISON_FORM: Final = "NFC"
WIDTH_VARIANT_TAGS = frozenset({"<wide>", "<narrow>"})
HEXADECIMAL = 16


def is_invisible_character(character: str) -> bool:
    return (
        unicodedata.category(character) in INVISIBLE_CATEGORIES
        or character in BLANK_LOOKING_CHARACTERS
        or any(ord(character) in ignorable for ignorable in DEFAULT_IGNORABLE_RANGES)
    )


def is_unstorable_character(character: str) -> bool:
    return character == NUL or unicodedata.category(character) == SURROGATE_CATEGORY


def strip_unstorable(value: str) -> str:
    return "".join(character for character in value if not is_unstorable_character(character))


def is_blank_character(character: str) -> bool:
    return (
        character.isspace()
        or unicodedata.category(character) in MARK_CATEGORIES
        or is_invisible_character(character)
    )


def is_blank(value: str) -> bool:
    return all(is_blank_character(character) for character in value)


def visible_length(value: str) -> int:
    return sum(1 for character in value if not is_blank_character(character))


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // UTF16_CODE_UNIT_BYTES


def is_uuid_v4(value: UUID) -> bool:
    return value.version == UUID_VERSION


def is_insignificant_character(character: str) -> bool:
    return is_invisible_character(character) and ord(character) not in EMOJI_TAG_CHARACTERS


def fold_width(character: str) -> str:
    tag, _, mapping = unicodedata.decomposition(character).partition(" ")
    if tag not in WIDTH_VARIANT_TAGS:
        return character
    return "".join(chr(int(code, HEXADECIMAL)) for code in mapping.split())


def fold_case_simply(character: str) -> str:
    for folded in (character.casefold(), character.lower()):
        if len(folded) == 1:
            return folded
    return character


def normalise_for_comparison(value: str) -> str:
    visible = "".join(
        fold_width(character)
        for character in value
        if character.isspace() or not is_insignificant_character(character)
    )
    words = " ".join(unicodedata.normalize(COMPARISON_FORM, visible).split())
    return unicodedata.normalize(COMPARISON_FORM, "".join(fold_case_simply(character) for character in words))


def is_web_url(value: str) -> bool:
    if any(character.isspace() or not character.isprintable() for character in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in WEB_URL_SCHEMES and bool(parts.hostname)


def is_timezone_aware(value: datetime) -> bool:
    return value.utcoffset() is not None
