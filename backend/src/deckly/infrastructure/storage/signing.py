import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
TERMINATOR = "aws4_request"
SIGNING_KEY_PREFIX = "AWS4"
DATE_FORMAT = "%Y%m%d"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
UNRESERVED = "-_.~"
PATH_SEPARATOR = "/"
DATE_HEADER = "x-amz-date"
CONTENT_SHA256_HEADER = "x-amz-content-sha256"
AUTHORIZATION_HEADER = "authorization"
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class SigningCredentials:
    access_key_id: str
    secret_access_key: str = field(repr=False)
    region: str


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    payload_sha256: str


def encode_path(path: str) -> str:
    return PATH_SEPARATOR.join(quote(segment, safe=UNRESERVED) for segment in path.split(PATH_SEPARATOR))


def canonical_query(query: Mapping[str, str]) -> str:
    pairs = sorted(
        (quote(name, safe=UNRESERVED), quote(value, safe=UNRESERVED)) for name, value in query.items()
    )
    return "&".join(f"{name}={value}" for name, value in pairs)


def normalised_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name.lower(): " ".join(value.split()) for name, value in sorted(headers.items())}


def signed_header_names(headers: Mapping[str, str]) -> str:
    return ";".join(sorted(headers))


def canonical_form(request: CanonicalRequest, headers: Mapping[str, str]) -> str:
    return "\n".join(
        (
            request.method,
            encode_path(request.path),
            canonical_query(request.query),
            "".join(f"{name}:{headers[name]}\n" for name in sorted(headers)),
            signed_header_names(headers),
            request.payload_sha256,
        )
    )


def credential_scope(day: str, region: str) -> str:
    return f"{day}/{region}/{SERVICE}/{TERMINATOR}"


def hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signing_key(secret_access_key: str, day: str, region: str) -> bytes:
    key = hmac_sha256(f"{SIGNING_KEY_PREFIX}{secret_access_key}".encode(), day)
    for part in (region, SERVICE, TERMINATOR):
        key = hmac_sha256(key, part)
    return key


def string_to_sign(timestamp: str, scope: str, canonical: str) -> str:
    return "\n".join((ALGORITHM, timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))


def signature_headers(
    request: CanonicalRequest, credentials: SigningCredentials, now: datetime
) -> dict[str, str]:
    moment = now.astimezone(UTC)
    timestamp = moment.strftime(TIMESTAMP_FORMAT)
    day = moment.strftime(DATE_FORMAT)
    stamped = {
        **request.headers,
        DATE_HEADER: timestamp,
        CONTENT_SHA256_HEADER: request.payload_sha256,
    }
    headers = normalised_headers(stamped)
    scope = credential_scope(day, credentials.region)
    signature = hmac.new(
        signing_key(credentials.secret_access_key, day, credentials.region),
        string_to_sign(timestamp, scope, canonical_form(request, headers)).encode(),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_header_names(headers)}, Signature={signature}"
    )
    return {
        DATE_HEADER: timestamp,
        CONTENT_SHA256_HEADER: request.payload_sha256,
        AUTHORIZATION_HEADER: authorization,
    }
