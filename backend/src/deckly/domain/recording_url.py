import re
from dataclasses import dataclass

from deckly.domain.exceptions import UnsupportedRecordingUrlError

MAX_RECORDING_URL_LENGTH = 2048
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
WEB_URL = re.compile(
    r"(?P<scheme>[A-Za-z]+)://(?P<host>[A-Za-z0-9.-]+)(?P<path>/[^?#]*)?(?:\?(?P<query>[^#]*))?(?:#.*)?"
)
VISIBLE_ASCII = re.compile(r"[!-~]+")
WEB_SCHEMES = frozenset({"http", "https"})
YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
SHORT_LINK_HOST = "youtu.be"
WATCH_PATHS = frozenset({"/watch", "/watch/"})
VIDEO_PATH = re.compile(r"/(?:shorts|live|embed)/(?P<id>[^/]+)/?")
SHORT_LINK_PATH = re.compile(r"/(?P<id>[^/]+)/?")
VIDEO_PARAMETER = "v"
PARAMETER_SEPARATOR = "&"
KEY_SEPARATOR = "="
CANONICAL_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


def unsupported(reason: str) -> UnsupportedRecordingUrlError:
    return UnsupportedRecordingUrlError(f"the url is not a single YouTube video: {reason}")


@dataclass(frozen=True, slots=True)
class YouTubeVideo:
    video_id: str

    def __post_init__(self) -> None:
        if VIDEO_ID.fullmatch(self.video_id) is None:
            message = "a YouTube video id is 11 letters, digits, hyphens or underscores"
            raise unsupported(message)

    @property
    def watch_url(self) -> str:
        return CANONICAL_WATCH_URL.format(video_id=self.video_id)


def video_parameter(query: str | None) -> str:
    values = [
        value
        for key, _, value in (
            part.partition(KEY_SEPARATOR) for part in (query or "").split(PARAMETER_SEPARATOR)
        )
        if key == VIDEO_PARAMETER
    ]
    if len(values) != 1:
        message = "a watch link needs exactly one v parameter"
        raise unsupported(message)
    return values[0]


def youtube_video_id(path: str, query: str | None) -> str:
    if path in WATCH_PATHS:
        return video_parameter(query)
    match = VIDEO_PATH.fullmatch(path)
    if match is None:
        message = "only watch, shorts, live and embed links name one video"
        raise unsupported(message)
    return match["id"]


def short_link_video_id(path: str) -> str:
    match = SHORT_LINK_PATH.fullmatch(path)
    if match is None:
        message = "a youtu.be link is the video id alone"
        raise unsupported(message)
    return match["id"]


def parse_youtube_video(raw: str) -> YouTubeVideo:
    url = raw.strip()
    if len(url) > MAX_RECORDING_URL_LENGTH or VISIBLE_ASCII.fullmatch(url) is None:
        message = f"it must be at most {MAX_RECORDING_URL_LENGTH} visible ASCII characters"
        raise unsupported(message)
    match = WEB_URL.fullmatch(url)
    if match is None or match["scheme"].lower() not in WEB_SCHEMES:
        message = "it must be an http or https link with a plain host name"
        raise unsupported(message)
    host = match["host"].lower()
    path = match["path"] or ""
    if host in YOUTUBE_HOSTS:
        return YouTubeVideo(youtube_video_id(path, match["query"]))
    if host == SHORT_LINK_HOST:
        return YouTubeVideo(short_link_video_id(path))
    message = "only youtube.com and youtu.be links are supported"
    raise unsupported(message)
