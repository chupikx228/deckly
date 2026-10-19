from collections.abc import Awaitable, Callable

type ProgressReport = Callable[[float], Awaitable[None]]


async def no_progress(fraction: float) -> None:
    del fraction
