import asyncio
import io
import json
import logging
import math
import socket
from collections.abc import Iterator
from uuid import UUID, uuid1, uuid4

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from opentelemetry.trace import format_span_id, format_trace_id

from deckly.application.correlation import CLIENT_ID, JOB_ID, REQUEST_ID, correlated, current_correlation
from deckly.application.exceptions import UpstreamUnavailableError
from deckly.infrastructure.llm.client import LlmPrompt, LlmReply, LlmStop, LlmUnavailableError
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.logging import URL_LOGGING_LIBRARY_LOGGERS, configure_logging
from deckly.infrastructure.observability.exposition import MetricsExposition, serve_metrics
from deckly.infrastructure.observability.http import ObservedRequests, client_id_from
from deckly.infrastructure.observability.runtime import Observability, create_observability
from deckly.infrastructure.observability.tracing import (
    continued_trace,
    current_trace_carrier,
    trace_carrier_from,
)
from deckly.infrastructure.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ProviderOperation,
    ResilientCaller,
    RetryPolicy,
)
from deckly.worker.settings import RUN_GENERATION_KEY, run_generation
from tests.fakes import (
    ADDRESS,
    RESET_SECONDS,
    TEST_SERVICE_NAME,
    FakeLlmClient,
    Harness,
    ManualTime,
    fresh_observability,
    generation_request,
    scope,
)
from tests.logs import captured_json_logs

pytestmark = pytest.mark.anyio

JOB = UUID("8f1d3a52-6c2b-4e0f-9a7d-1b2c3d4e5f60")
CLIENT = UUID("1c6e2f0a-3b4d-4c5e-8f6a-7b8c9d0e1f2a")
PROMPT = LlmPrompt(system="system", user="user", expected_output_tokens=10)
REPLY = LlmReply(text="{}", stop=LlmStop.COMPLETE)
POLICY = RetryPolicy(
    max_attempts=3,
    attempt_timeout_seconds=10,
    deadline_seconds=100,
    base_delay_seconds=1,
    max_delay_seconds=8,
)
OPERATION = ProviderOperation.CARD_GENERATION
QUIET_PATH = "/metrics"


@pytest.fixture
def restored_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    access = logging.getLogger("uvicorn.access")
    access_handlers, access_propagate = access.handlers[:], access.propagate
    libraries = {name: logging.getLogger(name).level for name in URL_LOGGING_LIBRARY_LOGGERS}
    try:
        yield
    finally:
        root.handlers, root.level = handlers, level
        access.handlers, access.propagate = access_handlers, access_propagate
        for name, library_level in libraries.items():
            logging.getLogger(name).setLevel(library_level)


def sample(observability: Observability, name: str, **labels: str) -> float | None:
    return observability.metrics.registry.get_sample_value(name, labels)


def resilient(
    observability: Observability, inner: FakeLlmClient, time: ManualTime, failure_threshold: int = 5
) -> tuple[ResilientLlmClient, CircuitBreaker]:
    probe = observability.probe(OPERATION)
    breaker = CircuitBreaker(
        failure_threshold=failure_threshold, reset_seconds=RESET_SECONDS, clock=time.clock, probe=probe
    )
    caller = ResilientCaller(POLICY, breaker, time.runtime(), probe)
    return ResilientLlmClient(inner, caller, 1000), breaker


def test_correlation_fields_from_the_context_are_added_to_every_line() -> None:
    with captured_json_logs() as logs, correlated(JOB_ID, JOB), correlated(CLIENT_ID, CLIENT):
        logging.getLogger("deckly.test").info("something_happened")

    [line] = logs.lines()
    assert (line["job_id"], line["client_id"]) == (str(JOB), str(CLIENT))


def test_a_field_passed_explicitly_wins_over_the_context() -> None:
    with captured_json_logs() as logs, correlated(JOB_ID, JOB):
        logging.getLogger("deckly.test").info("stale_job_failed", extra={"job_id": "explicit"})

    [line] = logs.lines()
    assert line["job_id"] == "explicit"


def test_a_binding_is_undone_when_its_block_ends_even_when_nested() -> None:
    with correlated(JOB_ID, JOB):
        with correlated(REQUEST_ID, CLIENT):
            assert dict(current_correlation()) == {"job_id": str(JOB), "request_id": str(CLIENT)}
        assert dict(current_correlation()) == {"job_id": str(JOB)}
    assert dict(current_correlation()) == {}


async def test_concurrent_jobs_never_see_each_others_job_id() -> None:
    seen: dict[UUID, str] = {}

    async def run(job_id: UUID) -> None:
        with correlated(JOB_ID, job_id):
            await asyncio.sleep(0)
            seen[job_id] = current_correlation()[JOB_ID]

    jobs = [uuid4() for _ in range(5)]
    await asyncio.gather(*(run(job_id) for job_id in jobs))

    assert seen == {job_id: str(job_id) for job_id in jobs}


def test_lines_written_inside_a_span_carry_its_trace_and_span_ids() -> None:
    tracer = fresh_observability().tracer

    with captured_json_logs() as logs, tracer.start_as_current_span("work") as span:
        logging.getLogger("deckly.test").info("inside")
    logging.getLogger("deckly.test").info("outside")

    [line] = logs.lines()
    context = span.get_span_context()
    assert line["trace_id"] == format_trace_id(context.trace_id)
    assert line["span_id"] == format_span_id(context.span_id)


@pytest.mark.usefixtures("restored_logging")
def test_configured_logging_drops_the_uvicorn_access_line_that_carries_the_client_address() -> None:
    configure_logging("INFO")

    assert not logging.getLogger("uvicorn.access").hasHandlers()
    assert logging.getLogger("uvicorn.error").hasHandlers()


@pytest.mark.usefixtures("restored_logging")
@pytest.mark.parametrize("library", ["httpx", "httpx2"])
def test_configured_logging_keeps_http_clients_from_logging_every_url_they_request(library: str) -> None:
    configure_logging("DEBUG")

    assert not logging.getLogger(library).isEnabledFor(logging.INFO)
    assert logging.getLogger(library).isEnabledFor(logging.WARNING)


async def test_every_provider_attempt_is_timed_and_logged_with_its_operation_and_outcome() -> None:
    observability = fresh_observability()
    time = ManualTime()
    client, _ = resilient(observability, FakeLlmClient(LlmUnavailableError("503"), REPLY), time)

    with captured_json_logs() as logs:
        await client.complete(PROMPT)

    attempts = logs.named("provider_call_finished")
    assert [(line["operation"], line["attempt"], line["outcome"]) for line in attempts] == [
        (OPERATION, 1, "transient"),
        (OPERATION, 2, "ok"),
    ]
    assert attempts[0]["error"] == "LlmUnavailableError"
    assert [line["operation"] for line in logs.named("provider_call_retrying")] == [OPERATION]
    assert (
        sample(
            observability, "deckly_provider_call_duration_seconds_count", operation=OPERATION, outcome="ok"
        )
        == 1
    )
    assert (
        sample(
            observability,
            "deckly_provider_call_duration_seconds_count",
            operation=OPERATION,
            outcome="transient",
        )
        == 1
    )
    assert sample(observability, "deckly_provider_call_retries_total", operation=OPERATION) == 1


async def test_an_attempt_that_times_out_is_recorded_as_a_timeout() -> None:
    observability = fresh_observability()
    time = ManualTime()

    async def too_slow() -> LlmReply:
        raise TimeoutError

    client, _ = resilient(observability, FakeLlmClient(too_slow), time)

    with pytest.raises(UpstreamUnavailableError):
        await client.complete(PROMPT)

    assert (
        sample(
            observability,
            "deckly_provider_call_duration_seconds_count",
            operation=OPERATION,
            outcome="timeout",
        )
        == POLICY.max_attempts
    )


async def test_the_circuit_gauge_follows_the_breaker_and_refused_calls_are_counted() -> None:
    observability = fresh_observability()
    time = ManualTime()
    client, breaker = resilient(
        observability, FakeLlmClient(LlmUnavailableError("503"), REPLY), time, failure_threshold=1
    )
    assert sample(observability, "deckly_provider_circuit_open", operation=OPERATION) == 0

    with pytest.raises(UpstreamUnavailableError):
        await client.complete(PROMPT)
    assert breaker.state is CircuitState.OPEN
    assert sample(observability, "deckly_provider_circuit_open", operation=OPERATION) == 1

    assert sample(observability, "deckly_provider_circuit_rejections_total", operation=OPERATION) == 1

    with captured_json_logs() as logs, pytest.raises(CircuitOpenError):
        await client.complete(PROMPT)
    assert sample(observability, "deckly_provider_circuit_rejections_total", operation=OPERATION) == 2
    assert [line["operation"] for line in logs.named("provider_call_refused")] == [OPERATION]

    time.now += RESET_SECONDS
    with captured_json_logs() as logs:
        await client.complete(PROMPT)
    assert sample(observability, "deckly_provider_circuit_open", operation=OPERATION) == 0
    assert [line["operation"] for line in logs.named("circuit_closed")] == [OPERATION]


def observed_app(observability: Observability) -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.get("/generations/{job_id}")
    async def get_generation(job_id: str) -> dict[str, str]:
        logging.getLogger("deckly.test").info("handled")
        return {"jobId": job_id}

    @router.get("/items/{number:int}/{rest:path}")
    async def nested(number: int, rest: str) -> dict[str, str]:
        return {"item": f"{number}/{rest}"}

    app.include_router(router, prefix="/v1")

    @app.get("/boom")
    async def boom() -> None:
        message = "handler bug"
        raise RuntimeError(message)

    @app.get(QUIET_PATH)
    async def metrics() -> str:
        return "ok"

    app.add_middleware(ObservedRequests, tracer=observability.tracer, quiet_paths=frozenset({QUIET_PATH}))
    return app


def test_a_request_is_logged_by_route_template_with_status_duration_and_client_but_no_address() -> None:
    client = TestClient(observed_app(fresh_observability()))

    with captured_json_logs() as logs:
        response = client.get(f"/v1/generations/{JOB}?secret=canary", headers={"X-Client-Id": str(CLIENT)})

    assert response.status_code == 200
    [handled] = logs.named("handled")
    [finished] = logs.named("http_request_finished")
    assert (finished["method"], finished["route"], finished["status"]) == (
        "GET",
        "/v1/generations/{job_id}",
        200,
    )
    assert isinstance(finished["duration_ms"], int)
    assert handled["client_id"] == finished["client_id"] == str(CLIENT)
    assert handled["trace_id"] == finished["trace_id"]
    assert str(JOB) not in json.dumps(finished)
    assert "canary" not in logs.text
    assert "testclient" not in logs.text


def test_an_unhandled_error_is_logged_as_a_server_error() -> None:
    client = TestClient(observed_app(fresh_observability()), raise_server_exceptions=False)

    with captured_json_logs() as logs:
        assert client.get("/boom").status_code == 500

    [finished] = logs.named("http_request_finished")
    assert (finished["route"], finished["status"]) == ("/boom", 500)


def test_unknown_routes_and_quiet_paths_never_leak_the_raw_path() -> None:
    client = TestClient(observed_app(fresh_observability()))

    with captured_json_logs() as logs:
        client.get("/not/a/route/canary")
        client.get(QUIET_PATH)

    assert [(line["route"], line["status"]) for line in logs.named("http_request_finished")] == [
        ("unmatched", 404)
    ]
    assert "canary" not in logs.text


PREFIXED = "/v1/items/{number}/{rest}"
UNPREFIXED = "/items/{number}/{rest}"


@pytest.mark.parametrize(
    ("path", "route"),
    [
        ("/v1/items/7/a/b", PREFIXED),
        ("/v1/items/7/v1", PREFIXED),
        ("/v1/items/7/%7Bnumber%7D", PREFIXED),
        ("/v1/items/007/canary", UNPREFIXED),
    ],
)
def test_a_router_prefix_is_kept_and_no_path_value_ever_reaches_the_route(path: str, route: str) -> None:
    client = TestClient(observed_app(fresh_observability()))

    with captured_json_logs() as logs:
        assert client.get(path).status_code == 200

    [finished] = logs.named("http_request_finished")
    assert finished["route"] == route


@pytest.mark.parametrize(
    "value",
    [
        str(CLIENT).upper().encode(),
        CLIENT.hex.encode(),
        f"{{{CLIENT}}}".encode(),
        f"urn:uuid:{CLIENT}".encode(),
        str(uuid1()).encode(),
        b"not-a-uuid",
        b"",
        "é".encode("latin-1"),
        b"x" * 10_000,
    ],
)
def test_only_a_canonical_version_four_client_id_is_trusted_for_correlation(value: bytes) -> None:
    assert client_id_from([(b"x-client-id", value)]) is None


def test_a_canonical_client_id_is_read_whatever_the_header_case() -> None:
    assert client_id_from([(b"X-Client-Id", str(CLIENT).encode())]) == CLIENT


async def test_queue_depth_is_read_on_every_scrape() -> None:
    observability = fresh_observability()
    exposition = MetricsExposition(observability.metrics, lambda: asyncio.sleep(0, result=7))

    text = (await exposition.render()).decode()

    assert 'deckly_generation_queue_depth{queue="generation"} 7.0' in text


async def test_an_unreadable_queue_is_exposed_as_not_a_number_rather_than_a_stale_depth() -> None:
    observability = fresh_observability()
    observability.metrics.queue_depth.labels(queue="generation").set(3)

    async def unavailable() -> int:
        raise UpstreamUnavailableError(1)

    await MetricsExposition(observability.metrics, unavailable).render()

    depth = sample(observability, "deckly_generation_queue_depth", queue="generation")
    assert depth is not None
    assert math.isnan(depth)


def test_a_process_that_never_reads_the_queue_exposes_no_depth_at_all() -> None:
    assert sample(fresh_observability(), "deckly_generation_queue_depth", queue="generation") is None


@pytest.mark.parametrize("value", [None, "traceparent", ["traceparent"], {"traceparent": 1}, {1: "x"}])
def test_a_malformed_trace_carrier_is_ignored(value: object) -> None:
    assert trace_carrier_from(value) == {}


def test_work_continued_from_a_carrier_joins_the_trace_that_produced_it() -> None:
    tracer = fresh_observability().tracer
    with tracer.start_as_current_span("POST /v1/generations") as request:
        carrier = current_trace_carrier()

    with continued_trace(trace_carrier_from(carrier)), tracer.start_as_current_span("generation.run") as run:
        pass

    assert run.get_span_context().trace_id == request.get_span_context().trace_id


def test_the_console_exporter_writes_one_json_line_per_finished_span() -> None:
    out = io.StringIO()
    tracer = create_observability(TEST_SERVICE_NAME, "console", out).tracer

    with tracer.start_as_current_span("parent"), tracer.start_as_current_span("child"):
        pass

    spans = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [span["name"] for span in spans] == ["child", "parent"]
    assert {span["resource"]["attributes"]["service.name"] for span in spans} == {TEST_SERVICE_NAME}


def test_an_unknown_http_method_is_never_copied_into_the_logs() -> None:
    client = TestClient(observed_app(fresh_observability()))

    with captured_json_logs() as logs:
        client.request("CANARYMETHOD", f"/v1/generations/{JOB}")

    [finished] = logs.named("http_request_finished")
    assert finished["method"] == "_OTHER"
    assert "CANARYMETHOD" not in logs.text


def test_a_worker_whose_metrics_port_is_taken_still_starts_and_says_why() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]

        with captured_json_logs() as logs:
            stop = serve_metrics(fresh_observability().metrics, "127.0.0.1", port)
        stop()

    [unavailable] = logs.named("worker_metrics_unavailable")
    assert (unavailable["port"], unavailable["error"]) == (port, "OSError")


def test_a_job_that_escapes_without_an_outcome_keeps_the_error_type_on_its_span() -> None:
    out = io.StringIO()
    telemetry = create_observability(TEST_SERVICE_NAME, "console", out).generation()

    def store_goes_away() -> None:
        with telemetry.run(JOB):
            message = "store went away"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        store_goes_away()

    [span] = [json.loads(line) for line in out.getvalue().splitlines()]
    assert span["status"]["description"] == "RuntimeError"
    assert span["attributes"]["deckly.outcome"] == "failed"


async def test_a_job_queued_before_trace_propagation_still_runs() -> None:
    harness = Harness()
    job_id = (await harness.create(generation_request(), scope(), ADDRESS)).job.job_id

    await run_generation({RUN_GENERATION_KEY: harness.run}, str(job_id))

    assert (await harness.get(job_id)).status == "succeeded"
