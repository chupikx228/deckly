from dataclasses import dataclass
from typing import ClassVar

from deckly.domain.exceptions import DistractorMatchesAnswerError, InvalidNoteError
from deckly.domain.notes.fields import NoteFields, require_text
from deckly.domain.notes.note_type import NoteType
from deckly.domain.text import normalise_for_comparison

MIN_DISTRACTORS = 2
MAX_DISTRACTORS = 4


@dataclass(frozen=True, slots=True)
class MultipleChoiceFields(NoteFields):
    note_type: ClassVar[NoteType] = NoteType.MULTIPLE_CHOICE

    question: str
    answer: str
    distractors: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.question, "question")
        require_text(self.answer, "answer")
        if not MIN_DISTRACTORS <= len(self.distractors) <= MAX_DISTRACTORS:
            message = (
                f"multiple choice needs {MIN_DISTRACTORS}-{MAX_DISTRACTORS} distractors, "
                f"got {len(self.distractors)}"
            )
            raise InvalidNoteError(message)
        for distractor in self.distractors:
            require_text(distractor, "distractor")
        normalised = [normalise_for_comparison(distractor) for distractor in self.distractors]
        if normalise_for_comparison(self.answer) in normalised:
            message = "a distractor matches the correct answer"
            raise DistractorMatchesAnswerError(message)
        if len(set(normalised)) != len(normalised):
            message = "distractors must be mutually exclusive"
            raise InvalidNoteError(message)

    @property
    def duplicate_key(self) -> tuple[str, str, frozenset[str]]:
        return (
            normalise_for_comparison(self.question),
            normalise_for_comparison(self.answer),
            frozenset(normalise_for_comparison(distractor) for distractor in self.distractors),
        )
