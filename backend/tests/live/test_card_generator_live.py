from uuid import uuid4

import pytest

from deckly.application.ports import SourceMaterial
from deckly.config import load_settings
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.worker.settings import build_llm_client

pytestmark = [pytest.mark.live, pytest.mark.anyio]

MATERIAL = (
    SourceMaterial(
        source=Source(title="Road sign basics", url="https://example.com/road-sign-basics"),
        text=(
            "Warning signs are triangles with a red border and warn drivers of a hazard ahead. "
            "Prohibitory signs are circles with a red border and forbid an action, such as entering "
            "or overtaking. The stop sign is a red octagon: drivers must come to a full stop before "
            "the line and give way. Mandatory signs are blue circles and tell drivers what they must do."
        ),
    ),
)
REQUEST = GenerationRequest(
    topic="Road signs",
    language="en",
    card_count=5,
    difficulty=Difficulty.BEGINNER,
    note_types=(NoteType.BASIC, NoteType.CLOZE, NoteType.MULTIPLE_CHOICE),
    include_images=False,
    instructions=None,
)


async def test_configured_provider_generates_valid_notes_that_cite_the_material() -> None:
    llm = build_llm_client(load_settings().providers)
    generator = LlmCardGenerator(llm=llm, new_id=uuid4, handlers=NOTE_TYPE_HANDLERS)
    try:
        result = await generator.generate(uuid4(), REQUEST, MATERIAL)
    finally:
        await llm.aclose()

    assert 0 < len(result.notes) <= REQUEST.card_count
    assert all(note.note_type in REQUEST.note_types for note in result.notes)
    assert all(note.sources == (MATERIAL[0].source,) for note in result.notes)
