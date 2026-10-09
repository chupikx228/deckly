from dataclasses import replace
from uuid import UUID

import pytest

from deckly.domain.media import Attribution, Media, MediaKind
from deckly.infrastructure.media.client import ImageCandidate
from deckly.infrastructure.media.licensing import (
    MAX_ALT_CHARACTERS,
    MAX_AUTHOR_CHARACTERS,
    MIN_IMAGE_SIDE,
    licensed_image,
)

MEDIA_ID = UUID(int=77, version=4)
THUMBNAIL = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Stop.svg/960px-Stop.svg.png"
CANDIDATE = ImageCandidate(
    file_title="File:Stop sign.svg",
    mime="image/svg+xml",
    thumbnail_url=THUMBNAIL,
    thumbnail_width=960,
    thumbnail_height=720,
    license_code="cc0",
    attribution_required="false",
    restrictions="",
    description="A red octagonal stop sign",
)
SOURCE_PAGE = "https://commons.wikimedia.org/wiki/File:Stop_sign.svg"
ATTRIBUTED = replace(
    CANDIDATE,
    license_code="cc-by-sa-3.0",
    attribution_required="true",
    description_url=SOURCE_PAGE,
    artist='<a href="//commons.wikimedia.org/wiki/User:Bidgee" title="User:Bidgee">Bidgee</a>',
)


def licensed(candidate: ImageCandidate) -> Media | None:
    return licensed_image(candidate, lambda: MEDIA_ID)


def test_public_domain_dedication_becomes_an_image_with_its_licence_alt_and_thumbnail() -> None:
    assert licensed(CANDIDATE) == Media(
        media_id=MEDIA_ID,
        kind=MediaKind.IMAGE,
        url=THUMBNAIL,
        license="CC0-1.0",
        alt="A red octagonal stop sign",
        width=960,
        height=720,
    )


@pytest.mark.parametrize(
    ("code", "licence"), [("cc0", "CC0-1.0"), ("pd", "Public-Domain"), (" PD ", "Public-Domain")]
)
def test_licences_that_need_no_attribution_are_mapped_to_a_fixed_name(code: str, licence: str) -> None:
    image = licensed(replace(CANDIDATE, license_code=code))

    assert image is not None
    assert image.license == licence


ATTRIBUTION_LICENCES = [
    (f"cc-{kind}-{version}", f"{name}-{version}", f"https://creativecommons.org/licenses/{kind}/{version}/")
    for kind, name in (("by", "CC-BY"), ("by-sa", "CC-BY-SA"))
    for version in ("1.0", "2.0", "2.5", "3.0", "4.0")
]


@pytest.mark.parametrize(("code", "licence", "deed"), ATTRIBUTION_LICENCES)
def test_generic_attribution_licences_are_accepted_with_their_spdx_name_and_deed(
    code: str, licence: str, deed: str
) -> None:
    image = licensed(replace(ATTRIBUTED, license_code=code))

    assert image is not None
    assert image.license == licence
    assert image.attribution is not None
    assert image.attribution.license_url == deed


def test_attribution_licence_becomes_an_image_that_credits_the_author_title_source_and_deed() -> None:
    assert licensed(ATTRIBUTED) == Media(
        media_id=MEDIA_ID,
        kind=MediaKind.IMAGE,
        url=THUMBNAIL,
        license="CC-BY-SA-3.0",
        alt="A red octagonal stop sign",
        width=960,
        height=720,
        attribution=Attribution(
            author="Bidgee",
            title="Stop sign",
            source_url=SOURCE_PAGE,
            license_url="https://creativecommons.org/licenses/by-sa/3.0/",
        ),
    )


def test_licences_that_need_no_attribution_carry_none() -> None:
    image = licensed(replace(ATTRIBUTED, license_code="pd", attribution_required="false"))

    assert image is not None
    assert image.attribution is None


def test_credit_line_the_author_asked_for_wins_over_the_artist_field() -> None:
    candidate = replace(
        ATTRIBUTED,
        credit_line="Photo by CEphoto, Uwe Aranas <b>or alternatively</b> © CEphoto, Uwe Aranas",
    )

    image = licensed(candidate)

    assert image is not None
    assert image.attribution is not None
    assert image.attribution.author == "Photo by CEphoto, Uwe Aranas or alternatively © CEphoto, Uwe Aranas"


@pytest.mark.parametrize("credit_line", [None, "", "  ", "<span> </span>"])
def test_blank_credit_line_falls_back_to_the_artist(credit_line: str | None) -> None:
    image = licensed(replace(ATTRIBUTED, credit_line=credit_line))

    assert image is not None
    assert image.attribution is not None
    assert image.attribution.author == "Bidgee"


@pytest.mark.parametrize("artist", ["Unknown Pleasures Archive", "Anonymous", "Author: Bidgee"])
def test_author_that_merely_contains_a_sentinel_word_is_kept(artist: str) -> None:
    image = licensed(replace(ATTRIBUTED, artist=artist))

    assert image is not None
    assert image.attribution is not None
    assert image.attribution.author == artist


def test_author_exactly_at_the_length_limit_is_kept() -> None:
    image = licensed(replace(ATTRIBUTED, artist="a" * MAX_AUTHOR_CHARACTERS))

    assert image is not None
    assert image.attribution is not None
    assert len(image.attribution.author) == MAX_AUTHOR_CHARACTERS


UNATTRIBUTABLE: dict[str, ImageCandidate] = {
    "no author at all": replace(ATTRIBUTED, artist=None),
    "blank author": replace(ATTRIBUTED, artist="   "),
    "author that is only markup": replace(ATTRIBUTED, artist="<a href='//x'></a><br/>"),
    "author that is only a script": replace(ATTRIBUTED, artist="<script>Bidgee</script>"),
    "unknown author": replace(ATTRIBUTED, artist="Unknown author"),
    "unknown author in markup": replace(ATTRIBUTED, artist='<span lang="en">Unknown</span>'),
    "unknown author in another case": replace(ATTRIBUTED, artist="UNKNOWN AUTHOR"),
    "unknown author with punctuation": replace(ATTRIBUTED, artist="Unknown author."),
    "unknown author in another word order": replace(ATTRIBUTED, artist="Author unknown"),
    "unknown author in parentheses": replace(ATTRIBUTED, artist="(unknown)"),
    "author longer than a credit line": replace(ATTRIBUTED, artist="a" * (MAX_AUTHOR_CHARACTERS + 1)),
    "no source page": replace(ATTRIBUTED, description_url=None),
    "source page off wikimedia": replace(
        ATTRIBUTED, description_url="https://example.com/wiki/File:Stop.svg"
    ),
    "source page over plain http": replace(ATTRIBUTED, description_url=SOURCE_PAGE.replace("https", "http")),
    "protocol-relative source page": replace(ATTRIBUTED, description_url="//commons.wikimedia.org/wiki/F"),
    "title with no visible words": replace(ATTRIBUTED, file_title="File:\u200b.svg"),
}


@pytest.mark.parametrize("candidate", UNATTRIBUTABLE.values(), ids=UNATTRIBUTABLE.keys())
def test_attribution_licence_whose_attribution_cannot_be_derived_is_dropped(
    candidate: ImageCandidate,
) -> None:
    assert licensed(candidate) is None


def test_unattributable_candidate_does_not_use_up_a_media_id() -> None:
    issued: list[UUID] = []

    def new_id() -> UUID:
        issued.append(MEDIA_ID)
        return MEDIA_ID

    licensed_image(replace(ATTRIBUTED, artist=None), new_id)

    assert issued == []


UNUSABLE_LICENCES: dict[str, ImageCandidate] = {
    "attribution licence claiming none is needed": replace(ATTRIBUTED, attribution_required="false"),
    "attribution licence with no attribution flag": replace(ATTRIBUTED, attribution_required=None),
    "ported attribution licence": replace(ATTRIBUTED, license_code="cc-by-sa-3.0-de"),
    "ported attribution licence with a region suffix": replace(ATTRIBUTED, license_code="cc-by-2.5-au"),
    "unknown attribution licence version": replace(ATTRIBUTED, license_code="cc-by-5.0"),
    "no-derivatives licence": replace(ATTRIBUTED, license_code="cc-by-nd-4.0"),
    "non-commercial share-alike licence": replace(ATTRIBUTED, license_code="cc-by-nc-sa-4.0"),
    "attribution licence with personality rights": replace(ATTRIBUTED, restrictions="personality"),
    "public domain that still asks for attribution": replace(
        CANDIDATE, license_code="pd", attribution_required="true"
    ),
    "attribution flag missing": replace(CANDIDATE, attribution_required=None),
    "attribution flag unreadable": replace(CANDIDATE, attribution_required="no"),
    "licence missing": replace(CANDIDATE, license_code=None),
    "licence blank": replace(CANDIDATE, license_code="  "),
    "licence unknown": replace(CANDIDATE, license_code="fal"),
    "non-commercial licence": replace(CANDIDATE, license_code="cc-by-nc-2.0"),
    "personality rights restriction": replace(CANDIDATE, restrictions="personality"),
    "trademark restriction": replace(CANDIDATE, restrictions="trademarked"),
    "several restrictions": replace(CANDIDATE, restrictions="insignia|trademarked"),
}


@pytest.mark.parametrize("candidate", UNUSABLE_LICENCES.values(), ids=UNUSABLE_LICENCES.keys())
def test_image_whose_licence_is_not_on_the_allowlist_or_contradicts_its_flags_is_dropped(
    candidate: ImageCandidate,
) -> None:
    assert licensed(candidate) is None


@pytest.mark.parametrize("restrictions", [None, "", "   "])
def test_absent_or_blank_restrictions_do_not_block_the_image(restrictions: str | None) -> None:
    assert licensed(replace(CANDIDATE, restrictions=restrictions)) is not None


UNUSABLE_PICTURES: dict[str, ImageCandidate] = {
    "no thumbnail": replace(CANDIDATE, thumbnail_url=None),
    "plain http": replace(CANDIDATE, thumbnail_url=THUMBNAIL.replace("https://", "http://")),
    "foreign host": replace(CANDIDATE, thumbnail_url="https://example.com/960px-Stop.svg.png"),
    "look-alike host": replace(CANDIDATE, thumbnail_url="https://upload.wikimedia.org.evil.test/a.png"),
    "suffix without a dot": replace(CANDIDATE, thumbnail_url="https://evilwikimedia.org/a.png"),
    "credentials in the url": replace(CANDIDATE, thumbnail_url="https://user:pw@upload.wikimedia.org/a.png"),
    "user name in the url": replace(CANDIDATE, thumbnail_url="https://user@upload.wikimedia.org/a.png"),
    "explicit port": replace(CANDIDATE, thumbnail_url="https://upload.wikimedia.org:8443/a.png"),
    "broken port": replace(CANDIDATE, thumbnail_url="https://upload.wikimedia.org:99999/a.png"),
    "whitespace in the url": replace(CANDIDATE, thumbnail_url="https://upload.wikimedia.org/a b.png"),
    "not a url": replace(CANDIDATE, thumbnail_url="upload.wikimedia.org/a.png"),
    "backslash that a browser reads as a path": replace(
        CANDIDATE, thumbnail_url="https://evil.test\\.wikimedia.org/a.png"
    ),
    "percent-encoded slash in the host": replace(
        CANDIDATE, thumbnail_url="https://evil.test%2F.wikimedia.org/a.png"
    ),
    "punctuation in the host": replace(CANDIDATE, thumbnail_url="https://evil.test;.wikimedia.org/a.png"),
    "underscore in the host": replace(CANDIDATE, thumbnail_url="https://evil_test.wikimedia.org/a.png"),
    "empty label in the host": replace(CANDIDATE, thumbnail_url="https://upload..wikimedia.org/a.png"),
    "bracketed host": replace(CANDIDATE, thumbnail_url="https://[::1].wikimedia.org/a.png"),
    "trailing dot": replace(CANDIDATE, thumbnail_url="https://upload.wikimedia.org./a.png"),
    "full-width dot": replace(CANDIDATE, thumbnail_url="https://evil.test\u3002wikimedia.org/a.png"),
    "pdf document": replace(CANDIDATE, mime="application/pdf"),
    "video": replace(CANDIDATE, mime="video/webm"),
    "unknown type": replace(CANDIDATE, mime=None),
    "too narrow": replace(CANDIDATE, thumbnail_width=MIN_IMAGE_SIDE - 1),
    "too short": replace(CANDIDATE, thumbnail_height=MIN_IMAGE_SIDE - 1),
    "unknown width": replace(CANDIDATE, thumbnail_width=None),
    "unknown height": replace(CANDIDATE, thumbnail_height=None),
}


@pytest.mark.parametrize("candidate", UNUSABLE_PICTURES.values(), ids=UNUSABLE_PICTURES.keys())
def test_picture_the_client_cannot_safely_download_and_show_is_dropped(candidate: ImageCandidate) -> None:
    assert licensed(candidate) is None


@pytest.mark.parametrize("host", ["upload.wikimedia.org", "thumb.wikimedia.org", "wikimedia.org"])
def test_wikimedia_hosts_are_trusted(host: str) -> None:
    assert (
        licensed(replace(CANDIDATE, thumbnail_url=f"https://{host}/a/ab/Stop.png?utm_source=x")) is not None
    )


def test_image_exactly_at_the_minimum_size_is_kept() -> None:
    candidate = replace(CANDIDATE, thumbnail_width=MIN_IMAGE_SIDE, thumbnail_height=MIN_IMAGE_SIDE)

    assert licensed(candidate) is not None


ALT_TEXTS: dict[str, tuple[str | None, str]] = {
    "plain description": ("A red stop sign", "A red stop sign"),
    "markup is removed": (
        '<div class="description en"><b>Stop</b> sign at a <a href="/x">crossing</a></div>',
        "Stop sign at a crossing",
    ),
    "entities are decoded": ("Stop &amp; yield signs", "Stop & yield signs"),
    "line breaks separate words": ("Stop<br>sign", "Stop sign"),
    "scripts and styles are not text": ("<style>p{}</style>Stop<script>alert(1)</script> sign", "Stop sign"),
    "invisible and unstorable characters go": ("Stop\u200b\x00 sign\u202e\ud800", "Stop sign"),
    "blank description falls back to the file name": ("<p> </p>", "Stop sign"),
    "missing description falls back to the file name": (None, "Stop sign"),
}


@pytest.mark.parametrize(("description", "alt"), ALT_TEXTS.values(), ids=ALT_TEXTS.keys())
def test_alt_text_is_the_plain_description_or_else_the_file_name(description: str | None, alt: str) -> None:
    image = licensed(replace(CANDIDATE, description=description))

    assert image is not None
    assert image.alt == alt


@pytest.mark.parametrize(
    ("title", "alt"),
    [
        ("File:Road_sign_A1.png", "Road sign A1"),
        ("File:Stop.sign.v2.jpg", "Stop.sign.v2"),
        ("File:NoExtension", "NoExtension"),
        ("Stop sign.svg", "Stop sign"),
    ],
)
def test_file_name_alt_text_drops_the_namespace_extension_and_underscores(title: str, alt: str) -> None:
    image = licensed(replace(CANDIDATE, file_title=title, description=None))

    assert image is not None
    assert image.alt == alt


@pytest.mark.parametrize("title", ["File:.png", "File:\u200b.png", "File:_.svg"])
def test_image_with_neither_a_description_nor_a_usable_file_name_is_dropped(title: str) -> None:
    assert licensed(replace(CANDIDATE, file_title=title, description="")) is None


def test_long_description_is_cut_at_a_word_to_the_alt_text_limit() -> None:
    image = licensed(replace(CANDIDATE, description="word " * 200))

    assert image is not None
    assert image.alt is not None
    assert len(image.alt) <= MAX_ALT_CHARACTERS
    assert image.alt.endswith("word\N{HORIZONTAL ELLIPSIS}")


def test_rejected_candidate_does_not_use_up_a_media_id() -> None:
    issued: list[UUID] = []

    def new_id() -> UUID:
        issued.append(MEDIA_ID)
        return MEDIA_ID

    licensed_image(replace(CANDIDATE, license_code="cc-by-4.0"), new_id)

    assert issued == []
