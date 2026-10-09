import re
from http import HTTPStatus

import pytest
from fastapi.routing import APIRoute

from deckly.main import ROUTERS
from deckly.transport.problem import ErrorCode
from tests.transport.openapi import declared_error_codes, declared_operations, declared_responses

PATH_PARAMETER = re.compile(r"\{[^}]+\}")


def without_parameter_names(path: str) -> str:
    return PATH_PARAMETER.sub("{}", path)


def served_routes() -> list[tuple[str, str, int]]:
    return [
        (without_parameter_names(route.path), method.lower(), route.status_code or HTTPStatus.OK)
        for router in ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    ]


def declared_pairs() -> set[tuple[str, str]]:
    return {(without_parameter_names(path), method) for path, method in declared_operations()}


def test_service_serves_exactly_the_operations_the_spec_declares() -> None:
    served = {(path, method) for path, method, _ in served_routes()}

    assert served == declared_pairs()


def test_every_error_code_the_service_can_emit_is_in_the_spec_and_the_other_way_round() -> None:
    assert {str(code) for code in ErrorCode} == declared_error_codes()


@pytest.mark.parametrize(("path", "method", "status"), served_routes())
def test_success_status_of_every_route_is_declared_in_the_spec(path: str, method: str, status: int) -> None:
    spec_path = next(
        spec_path
        for spec_path, spec_method in declared_operations()
        if spec_method == method and without_parameter_names(spec_path) == path
    )

    assert str(status) in declared_responses(spec_path, method)
