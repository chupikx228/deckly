import unicodedata
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

WEB_URL_SCHEMES = frozenset({"http", "https"})
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Mn", "Mc", "Me"})
UUID_VERSION = 4
UTF16_CODE_UNIT_BYTES = 2


def is_blank(value: str) -> bool:
    return all(
        character.isspace() or unicodedata.category(character) in INVISIBLE_CATEGORIES for character in value
    )


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // UTF16_CODE_UNIT_BYTES


def is_uuid_v4(value: UUID) -> bool:
    return value.version == UUID_VERSION


def normalise_for_comparison(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


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
