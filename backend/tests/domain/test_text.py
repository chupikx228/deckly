import pytest

from deckly.domain.text import is_blank, visible_length

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
