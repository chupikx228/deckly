import pytest

from deckly.domain.job import FailureCode
from deckly.transport.problem import ErrorCode
from tests.transport.openapi import spec_errors


@pytest.mark.parametrize("code", list(FailureCode))
def test_every_failure_code_is_a_wire_error_code(code: FailureCode) -> None:
    assert spec_errors("ErrorCode", str(code)) == []
    assert str(code) in {str(error_code) for error_code in ErrorCode}
