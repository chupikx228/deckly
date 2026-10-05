from dataclasses import dataclass
from enum import StrEnum

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

STAGE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)
JOB_BUCKETS = (0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 180.0, 240.0, 300.0, 600.0)
GENERATION_QUEUE = "generation"
PROVIDER_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 90.0, 180.0)


class CacheLookup(StrEnum):
    HIT = "hit"
    MISS = "miss"
    CORRUPT = "corrupt"
    ERROR = "error"


class RateLimit(StrEnum):
    GENERATION_CLIENT = "generation_client"
    GENERATION_ADDRESS = "generation_address"
    NOTE_REGENERATION = "note_regeneration"


@dataclass(frozen=True, slots=True)
class Metrics:
    registry: CollectorRegistry
    jobs_admitted: Counter
    jobs_cancelled: Counter
    jobs_finished: Counter
    job_duration: Histogram
    stage_duration: Histogram
    provider_call_duration: Histogram
    provider_retries: Counter
    provider_circuit_rejections: Counter
    provider_circuit_open: Gauge
    cache_lookups: Counter
    queue_depth: Gauge
    rate_limit_rejections: Counter


def create_metrics() -> Metrics:
    registry = CollectorRegistry()
    return Metrics(
        registry=registry,
        jobs_admitted=Counter(
            "deckly_generation_jobs_admitted",
            "Generation requests answered by POST /generations, by admission outcome.",
            ["outcome"],
            registry=registry,
        ),
        jobs_cancelled=Counter(
            "deckly_generation_jobs_cancelled",
            "Generation jobs cancelled by their client.",
            registry=registry,
        ),
        jobs_finished=Counter(
            "deckly_generation_jobs_finished",
            "Generation jobs a worker finished, by outcome and failure code.",
            ["outcome", "failure_code"],
            registry=registry,
        ),
        job_duration=Histogram(
            "deckly_generation_job_duration_seconds",
            "Wall time a worker spent on one generation job.",
            ["outcome"],
            buckets=JOB_BUCKETS,
            registry=registry,
        ),
        stage_duration=Histogram(
            "deckly_generation_stage_duration_seconds",
            "Time spent in one pipeline stage, by stage and outcome.",
            ["stage", "outcome"],
            buckets=STAGE_BUCKETS,
            registry=registry,
        ),
        provider_call_duration=Histogram(
            "deckly_provider_call_duration_seconds",
            "Duration of one attempt at an outbound provider call, by operation and outcome.",
            ["operation", "outcome"],
            buckets=PROVIDER_BUCKETS,
            registry=registry,
        ),
        provider_retries=Counter(
            "deckly_provider_call_retries",
            "Provider call attempts that were retried after a transient failure.",
            ["operation"],
            registry=registry,
        ),
        provider_circuit_rejections=Counter(
            "deckly_provider_circuit_rejections",
            "Provider calls refused without an attempt because the circuit was open.",
            ["operation"],
            registry=registry,
        ),
        provider_circuit_open=Gauge(
            "deckly_provider_circuit_open",
            "1 while the circuit breaker of a provider operation is open or half open.",
            ["operation"],
            registry=registry,
        ),
        cache_lookups=Counter(
            "deckly_generation_cache_lookups",
            "Generation result cache lookups, by result.",
            ["result"],
            registry=registry,
        ),
        queue_depth=Gauge(
            "deckly_generation_queue_depth",
            "Generation jobs waiting in the queue, read when the API is scraped.",
            ["queue"],
            registry=registry,
        ),
        rate_limit_rejections=Counter(
            "deckly_rate_limit_rejections",
            "Requests refused by a rate limit, by limit.",
            ["limit"],
            registry=registry,
        ),
    )
