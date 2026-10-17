from dataclasses import dataclass

from deckly.domain.exceptions import RecordingTooLongError


@dataclass(frozen=True, slots=True)
class DurationLimit:
    max_seconds: int

    def units(self, units_per_second: int) -> int:
        return self.max_seconds * units_per_second

    def too_long(self) -> RecordingTooLongError:
        return RecordingTooLongError(f"the recording is longer than the {self.max_seconds} second limit")

    def check(self, units: int, units_per_second: int) -> None:
        if units > self.units(units_per_second):
            raise self.too_long()
