import re
from dataclasses import dataclass
from types import MappingProxyType

from deckly.domain.exceptions import UnsupportedArticleUrlError

MAX_ARTICLE_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253
VISIBLE_ASCII = re.compile(r"[!-~]+")
ABSOLUTE_URL = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)(?P<path>/[^?#]*)?(?P<query>\?[^#]*)?(?:#.*)?"
)
AUTHORITY = re.compile(r"(?P<host>[^:@\[\]]+)(?::(?P<port>[0-9]{1,5}))?")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
TOP_LEVEL_LABEL = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
SECURE_SCHEME = "https"
INSECURE_SCHEME = "http"
STANDARD_PORTS = MappingProxyType({SECURE_SCHEME: 443, INSECURE_SCHEME: 80})
SPECIAL_USE_NAMES = ("localhost", "local", "internal", "test", "invalid", "example", "onion", "alt", "arpa")
LABEL_SEPARATOR = "."
ROOT_PATH = "/"
FRAGMENT_MARKER = "#"


def unsupported(reason: str) -> UnsupportedArticleUrlError:
    return UnsupportedArticleUrlError(f"the url cannot be fetched as an article: {reason}")


def is_special_use(host: str) -> bool:
    return any(host == name or host.endswith(f"{LABEL_SEPARATOR}{name}") for name in SPECIAL_USE_NAMES)


def is_public_host_name(host: str) -> bool:
    labels = host.split(LABEL_SEPARATOR)
    return (
        len(host) <= MAX_HOST_LENGTH
        and len(labels) > 1
        and all(HOST_LABEL.fullmatch(label) is not None for label in labels)
        and TOP_LEVEL_LABEL.fullmatch(labels[-1]) is not None
        and not is_special_use(host)
    )


@dataclass(frozen=True, slots=True)
class ArticleUrl:
    host: str
    target: str
    upgraded: bool = False

    def __post_init__(self) -> None:
        if not is_public_host_name(self.host):
            message = "the host must be a public domain name, not an address or a local name"
            raise unsupported(message)
        if (
            not self.target.startswith(ROOT_PATH)
            or FRAGMENT_MARKER in self.target
            or VISIBLE_ASCII.fullmatch(self.target) is None
        ):
            message = "the path must start with / and hold visible ASCII characters only"
            raise unsupported(message)
        if len(self.url) > MAX_ARTICLE_URL_LENGTH:
            message = f"it must be at most {MAX_ARTICLE_URL_LENGTH} characters"
            raise unsupported(message)

    @property
    def origin(self) -> str:
        return f"{SECURE_SCHEME}://{self.host}"

    @property
    def url(self) -> str:
        return f"{self.origin}{self.target}"


def parse_url(url: str, *, upgrade_insecure: bool) -> ArticleUrl:
    if len(url) > MAX_ARTICLE_URL_LENGTH or VISIBLE_ASCII.fullmatch(url) is None:
        message = f"it must be at most {MAX_ARTICLE_URL_LENGTH} visible ASCII characters"
        raise unsupported(message)
    match = ABSOLUTE_URL.fullmatch(url)
    if match is None:
        message = "it must be an absolute http or https link"
        raise unsupported(message)
    scheme = match["scheme"].lower()
    if scheme != SECURE_SCHEME and not (upgrade_insecure and scheme == INSECURE_SCHEME):
        message = "it must be an https link" if not upgrade_insecure else "it must be an http or https link"
        raise unsupported(message)
    authority = AUTHORITY.fullmatch(match["authority"])
    if authority is None:
        message = "it must name a host without credentials, and IP addresses are not accepted"
        raise unsupported(message)
    port = authority["port"]
    if port is not None and int(port) != STANDARD_PORTS[scheme]:
        message = "only the standard port of the scheme is fetched"
        raise unsupported(message)
    return ArticleUrl(
        host=authority["host"].lower().removesuffix(LABEL_SEPARATOR),
        target=(match["path"] or ROOT_PATH) + (match["query"] or ""),
        upgraded=scheme == INSECURE_SCHEME,
    )


def parse_article_url(raw: str) -> ArticleUrl:
    return parse_url(raw.strip(), upgrade_insecure=True)


def parse_redirect_url(raw: str) -> ArticleUrl:
    return parse_url(raw, upgrade_insecure=False)
