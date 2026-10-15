import asyncio
import logging
from collections.abc import Coroutine


class BoundedCalls:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        subject: str,
        timeout_error: type[TimeoutError],
        logger: logging.Logger,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._unresponsive_event = f"{subject}_unresponsive"
        self._abandoned_failure_event = f"abandoned_{subject}_call_failed"
        self._description = subject.replace("_", " ")
        self._timeout_error = timeout_error
        self._logger = logger
        self._abandoned: set[asyncio.Task[object]] = set()

    async def run[T](self, call: Coroutine[object, object, T]) -> T:
        task = asyncio.create_task(call)
        try:
            done, _ = await asyncio.wait({task}, timeout=self._timeout_seconds)
        except asyncio.CancelledError:
            self._abandon(task)
            raise
        if not done:
            self._abandon(task)
            self._logger.warning(self._unresponsive_event, extra={"timeout_seconds": self._timeout_seconds})
            message = f"the {self._description} did not answer within {self._timeout_seconds} seconds"
            raise self._timeout_error(message)
        return task.result()

    def _abandon(self, task: asyncio.Task[object]) -> None:
        task.cancel()
        self._abandoned.add(task)
        task.add_done_callback(self._settle)

    def _settle(self, task: asyncio.Task[object]) -> None:
        self._abandoned.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._logger.warning(self._abandoned_failure_event, extra={"error": type(error).__name__})
