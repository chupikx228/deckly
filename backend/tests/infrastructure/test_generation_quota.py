from datetime import UTC, datetime, timedelta, timezone

import pytest

from deckly.application.ports import Quota
from deckly.infrastructure.quota import (
    QUOTA_WINDOW,
    address_bucket,
    parse_reservation,
    parse_used,
    quota_from,
)
from deckly.infrastructure.rate_limit import seconds_until_window_end

DAY_START = datetime(2026, 8, 14, tzinfo=UTC)
NEXT_DAY = DAY_START + QUOTA_WINDOW
WINDOW_SECONDS = int(QUOTA_WINDOW.total_seconds())
LIMIT = 20


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:2:ffff:ffff:ffff:ffff"),
        ("2001:db8:1:2::1", "2001:0db8:0001:0002:0000:0000:0000:0009"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("fe80::1%eth0", "fe80::2"),
    ],
    ids=["one /64", "differently written", "ipv4-mapped", "scoped link-local"],
)
def test_addresses_one_host_can_rotate_through_share_a_bucket(first: str, second: str) -> None:
    assert address_bucket(first) == address_bucket(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:3::1"),
        ("203.0.113.9", "203.0.113.10"),
        ("::ffff:203.0.113.9", "203.0.113.10"),
    ],
    ids=["neighbouring /64", "neighbouring ipv4", "ipv4-mapped neighbour"],
)
def test_distinct_hosts_get_distinct_buckets(first: str, second: str) -> None:
    assert address_bucket(first) != address_bucket(second)


def test_ipv6_bucket_is_the_canonical_64_network() -> None:
    assert address_bucket("2001:DB8:1:2:3:4:5:6") == "2001:db8:1:2::/64"


@pytest.mark.parametrize("address", ["unknown", "testclient", ""])
def test_anything_that_is_not_an_ip_address_is_its_own_bucket(address: str) -> None:
    assert address_bucket(address) == address


@pytest.mark.parametrize(
    ("now", "resets_at"),
    [
        (DAY_START, NEXT_DAY),
        (DAY_START + timedelta(hours=12), NEXT_DAY),
        (NEXT_DAY - timedelta(microseconds=1), NEXT_DAY),
        (NEXT_DAY, NEXT_DAY + QUOTA_WINDOW),
        (datetime(2026, 8, 14, 23, 30, tzinfo=timezone(timedelta(hours=-5))), NEXT_DAY + QUOTA_WINDOW),
    ],
    ids=["midnight", "noon", "last microsecond", "next midnight", "offset clock"],
)
def test_quota_resets_at_the_next_utc_midnight(now: datetime, resets_at: datetime) -> None:
    assert quota_from(LIMIT, 0, now).resets_at == resets_at


def test_retry_after_counts_down_to_the_same_midnight() -> None:
    now = DAY_START + timedelta(hours=23, minutes=59, seconds=30)

    assert now + timedelta(seconds=seconds_until_window_end(now, WINDOW_SECONDS)) == NEXT_DAY


@pytest.mark.parametrize(("used", "remaining"), [(0, LIMIT), (1, LIMIT - 1), (LIMIT, 0), (LIMIT + 5, 0)])
def test_remaining_never_drops_below_zero(used: int, remaining: int) -> None:
    assert quota_from(LIMIT, used, DAY_START) == Quota(limit=LIMIT, remaining=remaining, resets_at=NEXT_DAY)


@pytest.mark.parametrize(("answer", "used"), [(None, 0), (b"0", 0), (b"17", 17)])
def test_stored_counter_is_read_as_jobs_used(answer: object, used: int) -> None:
    assert parse_used(answer) == used


@pytest.mark.parametrize("answer", [b"-1", b"1.5", b"", "3", 3, [b"3"]])
def test_counter_that_is_not_a_plain_count_is_refused(answer: object) -> None:
    with pytest.raises(TypeError):
        parse_used(answer)


@pytest.mark.parametrize("answer", [None, [0], [0, 1, 2], (0, 1), [b"0", 1], [0, "1"]])
def test_reservation_answer_of_the_wrong_shape_is_refused(answer: object) -> None:
    with pytest.raises(TypeError):
        parse_reservation(answer)
