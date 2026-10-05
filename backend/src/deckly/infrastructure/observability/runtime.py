import time
from dataclasses import dataclass
from typing import TextIO

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Tracer

from deckly.infrastructure.observability.metrics import Metrics, create_metrics
from deckly.infrastructure.observability.telemetry import ObservedGeneration
from deckly.infrastructure.observability.tracing import TRACER_NAME, TraceExporter, create_tracer_provider
from deckly.infrastructure.resilience import ProviderOperation, ProviderProbe


@dataclass(frozen=True, slots=True)
class Observability:
    metrics: Metrics
    tracer_provider: TracerProvider
    tracer: Tracer

    def generation(self) -> ObservedGeneration:
        return ObservedGeneration(metrics=self.metrics, tracer=self.tracer, clock=time.monotonic)

    def probe(self, operation: ProviderOperation) -> ProviderProbe:
        return ProviderProbe(operation=operation, metrics=self.metrics, tracer=self.tracer)

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()


def create_observability(service_name: str, exporter: TraceExporter, out: TextIO) -> Observability:
    provider = create_tracer_provider(service_name, exporter, out)
    return Observability(
        metrics=create_metrics(), tracer_provider=provider, tracer=provider.get_tracer(TRACER_NAME)
    )
