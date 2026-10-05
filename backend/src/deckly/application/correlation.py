from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from uuid import UUID

JOB_ID = "job_id"
CLIENT_ID = "client_id"
REQUEST_ID = "request_id"

NO_CORRELATION: Mapping[str, str] = MappingProxyType({})

_correlation: ContextVar[Mapping[str, str]] = ContextVar("correlation", default=NO_CORRELATION)


def current_correlation() -> Mapping[str, str]:
    return _correlation.get()


@contextmanager
def correlated(field: str, value: UUID) -> Iterator[None]:
    token = _correlation.set(MappingProxyType({**_correlation.get(), field: str(value)}))
    try:
        yield
    finally:
        _correlation.reset(token)
