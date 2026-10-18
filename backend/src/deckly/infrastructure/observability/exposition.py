import logging
import math
from collections.abc import Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, start_http_server

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.ports import QueueLane
from deckly.infrastructure.observability.metrics import Metrics

logger = logging.getLogger(__name__)

METRICS_PATH = "/metrics"
METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


class MetricsExposition:
    def __init__(self, metrics: Metrics, queue_depth: Callable[[QueueLane], Awaitable[int]]) -> None:
        self._metrics = metrics
        self._queue_depth = queue_depth

    async def render(self) -> bytes:
        for lane in QueueLane:
            try:
                depth = float(await self._queue_depth(lane))
            except UpstreamUnavailableError:
                depth = math.nan
            self._metrics.queue_depth.labels(queue=lane).set(depth)
        return generate_latest(self._metrics.registry)


def keep_running() -> None:
    return


def serve_metrics(metrics: Metrics, host: str, port: int) -> Callable[[], None]:
    try:
        server, thread = start_http_server(port, addr=host, registry=metrics.registry)
    except OSError as error:
        logger.exception(
            "worker_metrics_unavailable", extra={"host": host, "port": port, "error": type(error).__name__}
        )
        return keep_running

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join()

    return stop
