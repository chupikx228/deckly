import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus

from deckly.application.exceptions import UpstreamUnavailableError

logger = logging.getLogger(__name__)

MAX_BACKOFF_DOUBLINGS = 32
MIN_RETRY_AFTER_SECONDS = 1
TRANSIENT_STATUSES = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.CONFLICT, HTTPStatus.TOO_MANY_REQUESTS}
)
RETRY_AFTER_HEADER = "retry-after"


class TransientError(Exception):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PersistentError(Exception):
    pass


class CircuitOpenError(UpstreamUnavailableError):
    pass


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CallSize(StrEnum):
    TYPICAL = "typical"
    OVERSIZED = "oversized"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    attempt_timeout_seconds: float
    deadline_seconds: float
    base_delay_seconds: float
    max_delay_seconds: float

    def backoff_ceiling(self, attempt: int) -> float:
        doublings = min(attempt - 1, MAX_BACKOFF_DOUBLINGS)
        return min(self.max_delay_seconds, math.ldexp(self.base_delay_seconds, doublings))


@dataclass(frozen=True, slots=True)
class RetryRuntime:
    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    jitter: Callable[[], float]


def retry_after_hint(seconds: float) -> int:
    return max(MIN_RETRY_AFTER_SECONDS, math.ceil(seconds))


def is_transient_status(status: int) -> bool:
    return status in TRANSIENT_STATUSES or status >= HTTPStatus.INTERNAL_SERVER_ERROR


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int, reset_seconds: float, clock: Callable[[], float]) -> None:
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> CircuitState:
        return self._state

    def acquire(self) -> None:
        if self._state is CircuitState.CLOSED:
            return
        waited = self._clock() - self._opened_at
        if self._state is CircuitState.OPEN and waited >= self._reset_seconds:
            self._state = CircuitState.HALF_OPEN
            return
        raise CircuitOpenError(retry_after_hint(self._reset_seconds - waited))

    def record_success(self) -> None:
        if self._state is not CircuitState.CLOSED:
            logger.info("circuit_closed")
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN or self._consecutive_failures >= self._failure_threshold:
            self._open()

    def release(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN

    def _open(self) -> None:
        if self._state is not CircuitState.OPEN:
            logger.warning("circuit_opened", extra={"consecutive_failures": self._consecutive_failures})
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()


class ResilientCaller:
    def __init__(self, policy: RetryPolicy, breaker: CircuitBreaker, runtime: RetryRuntime) -> None:
        self._policy = policy
        self._breaker = breaker
        self._runtime = runtime

    async def call[T](self, operation: Callable[[], Awaitable[T]], *, size: CallSize) -> T:
        deadline = self._runtime.clock() + self._policy.deadline_seconds
        attempt = 1
        while True:
            self._breaker.acquire()
            try:
                result = await self._attempt(operation)
            except (TransientError, TimeoutError) as error:
                self._record_failure(error, size)
                delay = self._delay(attempt, error)
                if not self._may_retry(attempt, delay, deadline):
                    raise UpstreamUnavailableError(retry_after_hint(delay)) from error
                logger.warning(
                    "provider_call_retrying",
                    extra={
                        "attempt": attempt,
                        "delay_seconds": delay,
                        "error": type(error).__name__,
                        "call_size": size,
                    },
                )
                await self._runtime.sleep(delay)
                attempt += 1
                continue
            except asyncio.CancelledError:
                self._breaker.release()
                raise
            except PersistentError:
                self._breaker.record_failure()
                raise
            except Exception:
                self._breaker.record_success()
                raise
            self._breaker.record_success()
            return result

    async def _attempt[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        async with asyncio.timeout(self._policy.attempt_timeout_seconds):
            return await operation()

    def _record_failure(self, error: Exception, size: CallSize) -> None:
        if size is CallSize.OVERSIZED and isinstance(error, TimeoutError):
            self._breaker.release()
            return
        self._breaker.record_failure()

    def _delay(self, attempt: int, error: Exception) -> float:
        jittered = self._runtime.jitter() * self._policy.backoff_ceiling(attempt)
        requested = error.retry_after_seconds if isinstance(error, TransientError) else None
        return jittered if requested is None else max(jittered, requested)

    def _may_retry(self, attempt: int, delay: float, deadline: float) -> bool:
        next_attempt_ends = self._runtime.clock() + delay + self._policy.attempt_timeout_seconds
        return attempt < self._policy.max_attempts and next_attempt_ends <= deadline
