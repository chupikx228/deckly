import gc
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

COST_ROUNDS = 5
COST_SCALE = 8
MAX_FLAT_COST_GROWTH = 3
LINEAR_COST_SCALE = 16
MAX_LINEAR_COST_GROWTH = 4 * LINEAR_COST_SCALE
MAX_STOPPED_WALK_SHARE = 0.5


@contextmanager
def collector_paused() -> Iterator[None]:
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def cpu_costs(*runs: Callable[[], object]) -> list[float]:
    cheapest = [math.inf] * len(runs)
    with collector_paused():
        for _ in range(COST_ROUNDS):
            for index, run in enumerate(runs):
                started = time.process_time()
                run()
                cheapest[index] = min(cheapest[index], time.process_time() - started)
    return cheapest
