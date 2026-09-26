from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from deckly.domain.text import is_blank, strip_unstorable

type JsonObject = Mapping[str, object]

NO_OPTIONAL_KEYS: frozenset[str] = frozenset()


class UnusableOutputError(Exception):
    pass


def as_object(value: object) -> JsonObject | None:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return None


def require_keys(
    fields: JsonObject, required: AbstractSet[str], optional: AbstractSet[str] = NO_OPTIONAL_KEYS
) -> None:
    present = set(fields)
    missing = required - present
    unexpected = present - required - optional
    if missing or unexpected:
        message = f"fields do not match: missing {sorted(missing)}, unexpected {sorted(unexpected)}"
        raise UnusableOutputError(message)


def clean_text(value: str) -> str:
    return strip_unstorable(value).strip()


def read_text(fields: JsonObject, key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        message = f"{key} must be a string"
        raise UnusableOutputError(message)
    return clean_text(value)


def read_optional_text(fields: JsonObject, key: str) -> str:
    if fields.get(key) is None:
        return ""
    return read_text(fields, key)


def read_flag(fields: JsonObject, key: str) -> bool:
    value = fields.get(key)
    if not isinstance(value, bool):
        message = f"{key} must be a JSON boolean"
        raise UnusableOutputError(message)
    return value


def read_texts(fields: JsonObject, key: str) -> tuple[str, ...]:
    value = fields.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        message = f"{key} must be a list of strings"
        raise UnusableOutputError(message)
    return tuple(clean_text(item) for item in value)


def read_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    cleaned = (clean_text(item) for item in value if isinstance(item, str))
    return tuple(dict.fromkeys(label for label in cleaned if not is_blank(label)))
