import pytest

from deckly.domain.exceptions import UnsupportedRecordingUrlError
from deckly.domain.recording_url import MAX_RECORDING_URL_LENGTH, YouTubeVideo, parse_youtube_video

VIDEO_ID = "jNQXAC9IVRw"
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


@pytest.mark.parametrize(
    "url",
    [
        WATCH_URL,
        f"http://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/watch/?v={VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s",
        f"https://www.youtube.com/watch?feature=share&v={VIDEO_ID}&list=PL123",
        f"https://www.youtube.com/watch?v={VIDEO_ID}#t=10",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}/",
        f"https://www.youtube.com/live/{VIDEO_ID}?si=abc",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?si=share-token&t=5",
        f"  {WATCH_URL}\n",
    ],
)
def test_link_to_one_youtube_video_is_reduced_to_its_id(url: str) -> None:
    video = parse_youtube_video(url)

    assert video == YouTubeVideo(VIDEO_ID)
    assert video.watch_url == WATCH_URL


@pytest.mark.parametrize(
    "url",
    [
        "",
        VIDEO_ID,
        f"www.youtube.com/watch?v={VIDEO_ID}",
        f"//www.youtube.com/watch?v={VIDEO_ID}",
        f"ftp://www.youtube.com/watch?v={VIDEO_ID}",
        f"file:///etc/passwd?v={VIDEO_ID}",
        f"javascript://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com.evil.example/watch?v={VIDEO_ID}",
        f"https://evil.example/www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com@evil.example/watch?v={VIDEO_ID}",
        f"https://user:pass@www.youtube.com/watch?v={VIDEO_ID}",
        f"https://evil.example\\@www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com:443/watch?v={VIDEO_ID}",
        f"https://www.youtube.com:8080/watch?v={VIDEO_ID}",
        f"https://www.youtube.com./watch?v={VIDEO_ID}",
        f"https://youtube-nocookie.com/embed/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
        f"https://invidious.example/watch?v={VIDEO_ID}",
        f"https://studio.youtube.com/video/{VIDEO_ID}",
        f"https://127.0.0.1/watch?v={VIDEO_ID}",
        f"https://169.254.169.254/watch?v={VIDEO_ID}",
        f"https://[::1]/watch?v={VIDEO_ID}",
        f"https://www.y\N{CYRILLIC SMALL LETTER O}utube.com/watch?v={VIDEO_ID}",
        f"https://xn--www.ytube-yxe.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID} https://evil.example",
        f"https://www.youtube.com/watch?v=\t{VIDEO_ID}",
        f"https://www.youtube.com/wat\nch?v={VIDEO_ID}",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?list=PL1234567890",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&v={VIDEO_ID}",
        f"https://www.youtube.com/watch?V={VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}x",
        f"https://www.youtube.com/watch?v={VIDEO_ID[:10]}",
        "https://www.youtube.com/watch?v=jNQXAC9IVR%77",
        "https://www.youtube.com/watch?v=jNQXAC9IVR.",
        f"https://www.youtube.com/playlist?list=PL1234567890&v={VIDEO_ID}",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/channel/UC1234567890",
        f"https://www.youtube.com/shorts/{VIDEO_ID}/extra",
        f"https://www.youtube.com/v/{VIDEO_ID}",
        f"https://www.youtube.com/redirect?q=http://169.254.169.254/&v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}/extra",
        "https://youtu.be/",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&" + "a" * MAX_RECORDING_URL_LENGTH,
    ],
)
def test_anything_but_a_link_to_one_youtube_video_is_refused(url: str) -> None:
    with pytest.raises(UnsupportedRecordingUrlError):
        parse_youtube_video(url)


@pytest.mark.parametrize("video_id", ["", "short", "a" * 12, "jNQXAC9IVR/", "jNQXAC9IVR "])
def test_video_id_must_be_eleven_url_safe_characters(video_id: str) -> None:
    with pytest.raises(UnsupportedRecordingUrlError):
        YouTubeVideo(video_id)


def test_url_exactly_at_the_length_limit_is_read() -> None:
    padding = MAX_RECORDING_URL_LENGTH - len(WATCH_URL) - len("&p=")
    url = f"{WATCH_URL}&p={'a' * padding}"

    assert len(url) == MAX_RECORDING_URL_LENGTH
    assert parse_youtube_video(url).video_id == VIDEO_ID
