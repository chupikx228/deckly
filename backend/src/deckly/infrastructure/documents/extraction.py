import time
from collections.abc import Callable
from dataclasses import dataclass

from deckly.application.exceptions import UnreadableDocumentError

MAX_EXTRACTED_UNITS = 500

type MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ExtractedUnit:
    number: int
    text: str


@dataclass(frozen=True, slots=True)
class ExtractionDeadline:
    expires_at: float
    clock: MonotonicClock = time.monotonic

    @classmethod
    def after(cls, seconds: float, clock: MonotonicClock = time.monotonic) -> "ExtractionDeadline":
        return cls(expires_at=clock() + seconds, clock=clock)

    def check(self) -> None:
        if self.clock() > self.expires_at:
            message = "reading the document took longer than its extraction deadline"
            raise UnreadableDocumentError(message)

    def check_operation(self, *_: object) -> None:
        self.check()
