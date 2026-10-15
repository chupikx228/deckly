from datetime import UTC, datetime, timedelta, timezone

import pytest

from deckly.infrastructure.storage.signing import (
    EMPTY_PAYLOAD_SHA256,
    CanonicalRequest,
    SigningCredentials,
    encode_path,
    signature_headers,
)

AWS_DOCUMENTATION_SIGNING_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_EXAMPLE_CREDENTIALS = SigningCredentials(
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key=AWS_DOCUMENTATION_SIGNING_KEY,
    region="us-east-1",
)
AWS_EXAMPLE_TIME = datetime(2013, 5, 24, tzinfo=UTC)
AWS_EXAMPLE_HOST = "examplebucket.s3.amazonaws.com"
PUT_PAYLOAD_SHA256 = "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"


def signature(request: CanonicalRequest, now: datetime = AWS_EXAMPLE_TIME) -> str:
    return signature_headers(request, AWS_EXAMPLE_CREDENTIALS, now)["authorization"].rpartition("Signature=")[
        2
    ]


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        (
            CanonicalRequest(
                "GET", "/test.txt", {}, {"Host": AWS_EXAMPLE_HOST, "Range": "bytes=0-9"}, EMPTY_PAYLOAD_SHA256
            ),
            "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        ),
        (
            CanonicalRequest(
                "PUT",
                "/test$file.text",
                {},
                {
                    "Host": AWS_EXAMPLE_HOST,
                    "Date": "Fri, 24 May 2013 00:00:00 GMT",
                    "x-amz-storage-class": "REDUCED_REDUNDANCY",
                },
                PUT_PAYLOAD_SHA256,
            ),
            "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd",
        ),
        (
            CanonicalRequest("GET", "/", {"lifecycle": ""}, {"Host": AWS_EXAMPLE_HOST}, EMPTY_PAYLOAD_SHA256),
            "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543",
        ),
        (
            CanonicalRequest(
                "GET", "/", {"prefix": "J", "max-keys": "2"}, {"Host": AWS_EXAMPLE_HOST}, EMPTY_PAYLOAD_SHA256
            ),
            "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
        ),
    ],
    ids=["get object", "put object", "get bucket lifecycle", "list objects"],
)
def test_signature_matches_the_published_aws_example(request_: CanonicalRequest, expected: str) -> None:
    assert signature(request_) == expected


def test_signature_is_computed_on_the_utc_clock_whatever_offset_the_clock_reports() -> None:
    request = CanonicalRequest("GET", "/test.txt", {}, {"Host": AWS_EXAMPLE_HOST}, EMPTY_PAYLOAD_SHA256)
    moscow = AWS_EXAMPLE_TIME.astimezone(timezone(timedelta(hours=3)))

    assert signature(request, moscow) == signature(request)


def test_signed_headers_carry_the_date_the_payload_hash_and_the_credential_scope() -> None:
    request = CanonicalRequest("PUT", "/bucket/key", {}, {"host": AWS_EXAMPLE_HOST}, PUT_PAYLOAD_SHA256)

    headers = signature_headers(request, AWS_EXAMPLE_CREDENTIALS, AWS_EXAMPLE_TIME)

    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["x-amz-content-sha256"] == PUT_PAYLOAD_SHA256
    assert headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="
    )


def test_secret_never_appears_in_the_signed_headers_or_the_credentials_repr() -> None:
    request = CanonicalRequest("GET", "/", {}, {"host": AWS_EXAMPLE_HOST}, EMPTY_PAYLOAD_SHA256)

    headers = signature_headers(request, AWS_EXAMPLE_CREDENTIALS, AWS_EXAMPLE_TIME)

    secret = AWS_EXAMPLE_CREDENTIALS.secret_access_key
    assert all(secret not in value for value in headers.values())
    assert secret not in repr(AWS_EXAMPLE_CREDENTIALS)


@pytest.mark.parametrize(
    ("path", "encoded"),
    [
        ("/bucket/documents/0b6f7c1e", "/bucket/documents/0b6f7c1e"),
        ("/bucket/a b+c$", "/bucket/a%20b%2Bc%24"),
        ("/bucket/ключ", "/bucket/%D0%BA%D0%BB%D1%8E%D1%87"),
        ("/bucket/keep-_.~", "/bucket/keep-_.~"),
    ],
)
def test_path_segments_are_encoded_once_and_separators_are_kept(path: str, encoded: str) -> None:
    assert encode_path(path) == encoded
