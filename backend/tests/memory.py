import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def peak_memory() -> Iterator[list[int]]:
    peak: list[int] = []
    tracemalloc.start()
    try:
        yield peak
    finally:
        peak.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
