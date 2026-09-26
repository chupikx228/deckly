import pytest

from deckly.domain.text import is_blank, strip_unstorable, visible_length

BLANK_LOOKING = {
    "hangul filler": "\N{HANGUL FILLER}",
    "halfwidth hangul filler": "\N{HALFWIDTH HANGUL FILLER}",
    "hangul choseong filler": "\N{HANGUL CHOSEONG FILLER}",
    "hangul jungseong filler": "\N{HANGUL JUNGSEONG FILLER}",
    "braille pattern blank": "\N{BRAILLE PATTERN BLANK}",
    "musical symbol null notehead": "\N{MUSICAL SYMBOL NULL NOTEHEAD}",
    "reserved default ignorable in general punctuation": "⁥",
    "reserved default ignorable in specials": "￰",
    "unassigned tag plane code point": "\U000e0002",
    "last reserved tag plane code point": "\U000e0fff",
}

VISIBLE_LENGTHS = {
    "plain letters": ("abc", 3),
    "zero-width space between two letters": ("a\N{ZERO WIDTH SPACE}b", 2),
    "zero-width space before two letters": ("\N{ZERO WIDTH SPACE}ab", 2),
    "byte order mark after two letters": ("ab\N{ZERO WIDTH NO-BREAK SPACE}", 2),
    "hangul filler between two letters": ("a\N{HANGUL FILLER}b", 2),
    "braille blank between two letters": ("a\N{BRAILLE PATTERN BLANK}b", 2),
    "space between two letters": ("a b", 2),
    "combining accent counts with its letter": ("e\N{COMBINING ACUTE ACCENT}ab", 3),
    "devanagari spacing marks count with their consonant": ("हिन्दी", 3),
    "astral letters count once": ("\N{GRINNING FACE}" * 3, 3),
    "only invisibles": ("\N{ZERO WIDTH SPACE}\N{HANGUL FILLER}\N{BRAILLE PATTERN BLANK}", 0),
    "empty": ("", 0),
}


@pytest.mark.parametrize("character", BLANK_LOOKING.values(), ids=BLANK_LOOKING.keys())
def test_character_that_renders_blank_is_blank(character: str) -> None:
    assert is_blank(character)
    assert is_blank(f" {character}\N{ZERO WIDTH SPACE}{character} ")


@pytest.mark.parametrize("character", BLANK_LOOKING.values(), ids=BLANK_LOOKING.keys())
def test_blank_looking_character_does_not_hide_visible_text(character: str) -> None:
    assert not is_blank(f"{character}a{character}")


@pytest.mark.parametrize(
    "neighbour",
    ["\N{HANGUL SYLLABLE GA}", "\N{HANGUL CHOSEONG KIYEOK}", "\N{BRAILLE PATTERN DOTS-1}", "\U000e0100a"],
    ids=["hangul syllable", "hangul jamo", "braille with dots", "variation selector then letter"],
)
def test_visible_neighbours_of_blank_looking_characters_are_not_blank(neighbour: str) -> None:
    assert not is_blank(neighbour)


@pytest.mark.parametrize(("value", "expected"), VISIBLE_LENGTHS.values(), ids=VISIBLE_LENGTHS.keys())
def test_visible_length_counts_only_characters_that_render(value: str, expected: int) -> None:
    assert visible_length(value) == expected


UNSTORABLE = {
    "nul": "\N{NULL}",
    "lone high surrogate": "\ud800",
    "lone low surrogate": "\udfff",
}

STORABLE = {
    "line breaks and tabs": "Red\ntriangle\r\n\twarns",
    "other control characters": "\x01\x1f\x7f",
    "zero-width joiner inside an emoji sequence": "\N{WOMAN}\N{ZERO WIDTH JOINER}\N{PERSONAL COMPUTER}",
    "zero-width non-joiner in persian": "\u0645\u06cc\N{ZERO WIDTH NON-JOINER}\u062e\u0648\u0627\u0647\u0645",
    "right-to-left mark": "\N{RIGHT-TO-LEFT MARK}\u05e9\u05dc\u05d5\u05dd",
    "astral character": "\N{OCTAGONAL SIGN}",
    "byte order mark": "\N{ZERO WIDTH NO-BREAK SPACE}text",
}


@pytest.mark.parametrize("character", UNSTORABLE.values(), ids=UNSTORABLE.keys())
def test_unstorable_character_is_stripped_wherever_it_appears(character: str) -> None:
    assert strip_unstorable(f"{character}Red{character} triangle{character}") == "Red triangle"


@pytest.mark.parametrize("value", STORABLE.values(), ids=STORABLE.keys())
def test_strip_unstorable_leaves_every_other_character_alone(value: str) -> None:
    assert strip_unstorable(value) == value


def test_text_made_only_of_unstorable_characters_strips_to_empty() -> None:
    assert strip_unstorable("".join(UNSTORABLE.values()) * 3) == ""
