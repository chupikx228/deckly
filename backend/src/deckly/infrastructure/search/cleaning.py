import re
import unicodedata

from deckly.domain.text import is_invisible_character, is_unstorable_character, strip_unstorable

MAX_TITLE_CHARACTERS = 200
PROMPT_TAG = re.compile(r"<(\s*/?\s*source\b)", re.IGNORECASE)
NEUTRAL_ANGLE = "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}"
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
REPLACEMENT_CHARACTER = "\N{REPLACEMENT CHARACTER}"
WORD_SEPARATOR = " "
LINE_SEPARATOR = "\n"
GARBAGE_CATEGORIES = frozenset({"Cc", "Co", "Cn"})
SPELLING_JOINERS = frozenset({"\N{ZERO WIDTH NON-JOINER}", "\N{ZERO WIDTH JOINER}"})


def is_droppable_character(character: str) -> bool:
    return character not in SPELLING_JOINERS and is_invisible_character(character)


def visible_words(value: str) -> str:
    kept = "".join(
        WORD_SEPARATOR if character.isspace() else character
        for character in value
        if character.isspace() or not is_droppable_character(character)
    )
    return WORD_SEPARATOR.join(kept.split())


def neutralise_prompt_tags(value: str) -> str:
    return PROMPT_TAG.sub(rf"{NEUTRAL_ANGLE}\1", value)


def cut_at_word(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    window = value[: limit + 1]
    boundary = max(window.rfind(WORD_SEPARATOR), window.rfind(LINE_SEPARATOR))
    return (value[:boundary] if boundary > 0 else value[:limit]).rstrip()


def clean_title(value: str) -> str:
    title = neutralise_prompt_tags(visible_words(strip_unstorable(value)))
    if len(title) <= MAX_TITLE_CHARACTERS:
        return title
    return cut_at_word(title, MAX_TITLE_CHARACTERS - len(ELLIPSIS)) + ELLIPSIS


def clean_text(value: str, limit: int) -> str:
    lines = (visible_words(line) for line in strip_unstorable(value).splitlines())
    text = LINE_SEPARATOR.join(line for line in lines if line)
    return neutralise_prompt_tags(cut_at_word(text, limit))


def is_garbage_character(character: str) -> bool:
    return (
        character == REPLACEMENT_CHARACTER
        or is_unstorable_character(character)
        or (not character.isspace() and unicodedata.category(character) in GARBAGE_CATEGORIES)
    )


def garbage_share(value: str) -> float:
    if not value:
        return 0.0
    return sum(1 for character in value if is_garbage_character(character)) / len(value)
