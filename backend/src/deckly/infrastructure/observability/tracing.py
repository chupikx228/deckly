from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Literal, TextIO

from opentelemetry import context, propagate
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

type TraceExporter = Literal["none", "console"]
type TraceCarrier = dict[str, str]

TRACER_NAME = "deckly"


def compact_span(span: ReadableSpan) -> str:
    return f"{span.to_json(indent=None)}\n"


def create_tracer_provider(service_name: str, exporter: TraceExporter, out: TextIO) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    if exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=out, formatter=compact_span)))
    return provider


def current_trace_carrier() -> TraceCarrier:
    carrier: TraceCarrier = {}
    propagate.inject(carrier)
    return carrier


def trace_carrier_from(value: object) -> TraceCarrier:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}


@contextmanager
def continued_trace(carrier: TraceCarrier) -> Iterator[None]:
    token = context.attach(propagate.extract(carrier))
    try:
        yield
    finally:
        context.detach(token)
