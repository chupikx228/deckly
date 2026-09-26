from types import MappingProxyType

from deckly.domain.deck import MAX_DESCRIPTION_LENGTH, MAX_TITLE_LENGTH, Deck
from deckly.domain.text import is_blank_character, strip_unstorable, utf16_length
from deckly.infrastructure.card_generator.untrusted import JsonObject, as_object, read_labels

NO_DECK: JsonObject = MappingProxyType({})


def trim_blank(value: str) -> str:
    visible = [index for index, character in enumerate(value) if not is_blank_character(character)]
    return value[visible[0] : visible[-1] + 1] if visible else ""


def fit_utf16(value: str, limit: int) -> str:
    kept: list[str] = []
    used = 0
    for character in value:
        used += utf16_length(character)
        if used > limit:
            break
        kept.append(character)
    return "".join(kept)


def display_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return trim_blank(fit_utf16(trim_blank(strip_unstorable(value)), limit))


def repair_deck(value: object, topic: str) -> Deck:
    deck = as_object(value) or NO_DECK
    title = display_text(deck.get("title"), MAX_TITLE_LENGTH) or display_text(topic, MAX_TITLE_LENGTH)
    description = display_text(deck.get("description"), MAX_DESCRIPTION_LENGTH) or None
    return Deck(title=title, description=description, tags=read_labels(deck.get("tags")))
