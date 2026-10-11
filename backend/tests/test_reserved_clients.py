import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from deckly.domain.text import is_uuid_v4
from tests.reserved_clients import RESERVED_PATTERN, minted_at_or_after, reserved_client_id

MINTED = datetime(2026, 8, 14, 10, 30, tzinfo=UTC)
CANONICAL = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def matches_reserved_pattern(client_id: UUID) -> bool:
    return re.fullmatch(RESERVED_PATTERN.replace("_", "[0-9a-f]"), str(client_id)) is not None


def test_a_reserved_client_id_is_a_canonical_version_4_uuid_the_api_accepts() -> None:
    client_id = reserved_client_id()

    assert is_uuid_v4(client_id)
    assert CANONICAL.fullmatch(str(client_id)) is not None


def test_a_reserved_client_id_carries_the_reserved_namespace_and_its_minting_time() -> None:
    client_id = reserved_client_id(MINTED)

    assert matches_reserved_pattern(client_id)
    assert client_id.hex.startswith(f"{int(MINTED.timestamp()):08x}")


def test_reserved_client_ids_do_not_repeat() -> None:
    assert len({reserved_client_id(MINTED) for _ in range(1000)}) == 1000


def test_the_epoch_is_a_valid_minting_time() -> None:
    client_id = reserved_client_id(datetime(1970, 1, 1, tzinfo=UTC))

    assert str(client_id).startswith("00000000-")
    assert is_uuid_v4(client_id)


def test_a_minting_time_that_does_not_fit_thirty_two_bits_is_refused() -> None:
    with pytest.raises(ValueError, match="badly formed"):
        reserved_client_id(datetime(2200, 1, 1, tzinfo=UTC))


def test_random_version_4_uuids_never_look_reserved() -> None:
    assert not any(matches_reserved_pattern(uuid4()) for _ in range(10_000))


def test_the_age_bound_orders_below_every_id_minted_at_or_after_it() -> None:
    bound = minted_at_or_after(MINTED)

    assert reserved_client_id(MINTED) > bound
    assert reserved_client_id(datetime(2026, 8, 14, 10, 29, 59, tzinfo=UTC)) < bound
