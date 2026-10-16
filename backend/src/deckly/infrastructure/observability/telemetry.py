import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from opentelemetry.trace import Span, Status, StatusCode, Tracer

from deckly.application.exceptions import JobStoppedError
from deckly.application.ports import AdmissionOutcome, JobOutcome
from deckly.domain.generation import SourceKind
from deckly.domain.job import FailureCode, JobStage
from deckly.infrastructure.observability.metrics import Metrics

RUN_SPAN = "generation.run"
STAGE_SPAN_PREFIX = "generation.stage."
JOB_ID_ATTRIBUTE = "deckly.job_id"
STAGE_ATTRIBUTE = "deckly.stage"
OUTCOME_ATTRIBUTE = "deckly.outcome"
FAILURE_CODE_ATTRIBUTE = "deckly.failure_code"
SOURCE_KIND_ATTRIBUTE = "deckly.source_kind"
NO_FAILURE_CODE = ""
UNKNOWN_SOURCE = ""


class StageOutcome(StrEnum):
    OK = "ok"
    FAILED = "failed"
    STOPPED = "stopped"


def stage_outcome(error: BaseException) -> StageOutcome:
    if isinstance(error, JobStoppedError | asyncio.CancelledError):
        return StageOutcome.STOPPED
    return StageOutcome.FAILED


def mark_failed(span: Span, error: BaseException) -> None:
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))


class ObservedRun:
    def __init__(self, span: Span) -> None:
        self.span = span
        self.outcome = JobOutcome.FAILED
        self.code: FailureCode | None = None
        self.source: SourceKind | None = None
        self.finished = False

    def identify(self, source: SourceKind) -> None:
        self.source = source
        self.span.set_attribute(SOURCE_KIND_ATTRIBUTE, source)

    def finish(self, outcome: JobOutcome, code: FailureCode | None = None) -> None:
        if self.finished:
            return
        self.outcome = outcome
        self.code = code
        self.finished = True


@dataclass(frozen=True, slots=True)
class ObservedGeneration:
    metrics: Metrics
    tracer: Tracer
    clock: Callable[[], float]

    def admitted(self, outcome: AdmissionOutcome, source: SourceKind) -> None:
        self.metrics.jobs_admitted.labels(outcome=outcome, source=source).inc()

    def cancelled(self) -> None:
        self.metrics.jobs_cancelled.inc()

    @contextmanager
    def run(self, job_id: UUID) -> Iterator[ObservedRun]:
        started = self.clock()
        with self.tracer.start_as_current_span(
            RUN_SPAN,
            attributes={JOB_ID_ATTRIBUTE: str(job_id)},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            run = ObservedRun(span)
            try:
                yield run
            except BaseException as error:
                mark_failed(span, error)
                raise
            finally:
                self._finish_run(span, run, self.clock() - started)

    @contextmanager
    def stage(self, stage: JobStage, source: SourceKind) -> Iterator[None]:
        started = self.clock()
        outcome = StageOutcome.OK
        with self.tracer.start_as_current_span(
            f"{STAGE_SPAN_PREFIX}{stage}",
            attributes={STAGE_ATTRIBUTE: stage, SOURCE_KIND_ATTRIBUTE: source},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield
            except BaseException as error:
                outcome = stage_outcome(error)
                if outcome is StageOutcome.FAILED:
                    mark_failed(span, error)
                raise
            finally:
                span.set_attribute(OUTCOME_ATTRIBUTE, outcome)
                self.metrics.stage_duration.labels(stage=stage, outcome=outcome, source=source).observe(
                    self.clock() - started
                )

    def _finish_run(self, span: Span, run: ObservedRun, seconds: float) -> None:
        code = NO_FAILURE_CODE if run.code is None else run.code
        source = UNKNOWN_SOURCE if run.source is None else run.source
        span.set_attribute(OUTCOME_ATTRIBUTE, run.outcome)
        if run.code is not None:
            span.set_attribute(FAILURE_CODE_ATTRIBUTE, code)
            span.set_status(Status(StatusCode.ERROR, code))
        self.metrics.jobs_finished.labels(outcome=run.outcome, failure_code=code, source=source).inc()
        self.metrics.job_duration.labels(outcome=run.outcome, source=source).observe(seconds)
