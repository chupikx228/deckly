from uuid import UUID

import pytest

from deckly.domain.exceptions import InvalidMediaError, InvalidSourceError
from deckly.domain.media import Media, MediaKind
from deckly.domain.source import Source
from tests.domain.builders import T0, client_id

IMAGE_URL = "https://cdn.example.com/sign.png"
BAD_URLS = [
    "",
    "not a url",
    "https://",
    "ftp://example.com/file.png",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "https://exa mple.com/a.png",
    "https://example.com/a.png\n",
    "http://[::1",
    "//example.com/a.png",
]


def image(alt: str | None = "Warning sign", license_name: str = "CC-BY-4.0", url: str = IMAGE_URL) -> Media:
    return Media(media_id=client_id(1), kind=MediaKind.IMAGE, url=url, license=license_name, alt=alt)


def test_licensed_image_with_alt_text_is_accepted() -> None:
    assert image().alt == "Warning sign"


@pytest.mark.parametrize("alt", [None, "", "   "])
def test_image_without_alt_text_is_rejected(alt: str | None) -> None:
    with pytest.raises(InvalidMediaError):
        image(alt=alt)


def test_audio_does_not_require_alt_text() -> None:
    audio = Media(
        media_id=client_id(1), kind=MediaKind.AUDIO, url="https://cdn.example.com/a.mp3", license="CC0-1.0"
    )

    assert audio.alt is None


@pytest.mark.parametrize("license_name", ["", "  "])
def test_media_without_a_licence_is_rejected(license_name: str) -> None:
    with pytest.raises(InvalidMediaError):
        image(license_name=license_name)


@pytest.mark.parametrize("url", BAD_URLS)
def test_media_with_a_non_web_url_is_rejected(url: str) -> None:
    with pytest.raises(InvalidMediaError):
        image(url=url)


@pytest.mark.parametrize("dimension", ["width", "height"])
@pytest.mark.parametrize("value", [0, -1])
def test_media_dimensions_must_be_positive(dimension: str, value: int) -> None:
    with pytest.raises(InvalidMediaError):
        Media(
            media_id=client_id(1),
            kind=MediaKind.IMAGE,
            url=IMAGE_URL,
            license="CC0-1.0",
            alt="Sign",
            **{dimension: value},
        )


def test_media_id_that_is_not_uuid_v4_is_rejected() -> None:
    with pytest.raises(InvalidMediaError):
        Media(media_id=UUID(int=1), kind=MediaKind.IMAGE, url=IMAGE_URL, license="CC0-1.0", alt="Sign")


def test_media_dimension_of_one_pixel_is_accepted() -> None:
    media = Media(
        media_id=client_id(1),
        kind=MediaKind.IMAGE,
        url=IMAGE_URL,
        license="CC0-1.0",
        alt="Sign",
        width=1,
        height=1,
    )

    assert (media.width, media.height) == (1, 1)


def test_source_with_offset_timestamp_is_accepted() -> None:
    source = Source(title="Traffic regulations", url="https://example.com/rules", retrieved_at=T0)

    assert source.retrieved_at == T0


@pytest.mark.parametrize("url", BAD_URLS)
def test_source_with_a_non_web_url_is_rejected(url: str) -> None:
    with pytest.raises(InvalidSourceError):
        Source(title="Traffic regulations", url=url)


@pytest.mark.parametrize("title", ["", " \n"])
def test_source_with_a_blank_title_is_rejected(title: str) -> None:
    with pytest.raises(InvalidSourceError):
        Source(title=title, url="https://example.com/rules")


def test_source_retrieved_without_an_offset_is_rejected() -> None:
    with pytest.raises(InvalidSourceError):
        Source(title="Traffic regulations", url="https://example.com", retrieved_at=T0.replace(tzinfo=None))
