import math
from dataclasses import dataclass
from typing import ClassVar

from deckly.domain.exceptions import InvalidNoteError
from deckly.domain.notes.fields import NoteFields, require_text
from deckly.domain.notes.note_type import NoteType

MIN_ORDINAL = 1


@dataclass(frozen=True, slots=True)
class OcclusionRegion:
    ordinal: int
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if self.ordinal < MIN_ORDINAL:
            message = f"region ordinal must be at least {MIN_ORDINAL}, got {self.ordinal}"
            raise InvalidNoteError(message)
        for name, value in (("x", self.x), ("y", self.y), ("width", self.width), ("height", self.height)):
            if not (math.isfinite(value) and 0.0 <= value <= 1.0):
                message = f"region {name} must be normalised to 0-1, got {value}"
                raise InvalidNoteError(message)
        if self.width == 0.0 or self.height == 0.0:
            message = f"region must have a non-zero width and height, got {self.width} x {self.height}"
            raise InvalidNoteError(message)
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            message = "region must lie entirely within the image"
            raise InvalidNoteError(message)


@dataclass(frozen=True, slots=True)
class ImageOcclusionFields(NoteFields):
    note_type: ClassVar[NoteType] = NoteType.IMAGE_OCCLUSION

    image_id: str
    regions: tuple[OcclusionRegion, ...]
    extra: str = ""

    def __post_init__(self) -> None:
        require_text(self.image_id, "imageId")
        if not self.regions:
            message = "image occlusion needs at least one region"
            raise InvalidNoteError(message)
        ordinals = [region.ordinal for region in self.regions]
        if len(set(ordinals)) != len(ordinals):
            message = f"region ordinals must be unique, got {sorted(ordinals)}"
            raise InvalidNoteError(message)

    @property
    def referenced_image_ids(self) -> frozenset[str]:
        return frozenset({self.image_id})
