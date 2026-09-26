import unicodedata
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.pipeline import RunGeneration
from deckly.domain.job import GenerationJob, Succeeded
from deckly.domain.notes.basic import BasicFields
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.job_store import PostgresJobStore
from tests.domain.builders import T0
from tests.fakes import FakeLlmClient, FakeProviders, generation_request, model_reply
from tests.integration.conftest import Cleanup

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

UNSTORABLE = "\x00\udfff\ud800"
SURROGATE_CATEGORY = "Cs"


async def test_generated_text_with_nul_and_lone_surrogates_is_stored_and_read_back(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    store = PostgresJobStore(session_factory)
    job = GenerationJob.queue(uuid4(), T0)
    await store.add(job, generation_request(), cleanup.scope())
    note = {
        "noteType": NoteType.BASIC,
        "fields": {"front": f"Red{UNSTORABLE} triangle", "back": f"Warn{UNSTORABLE}ing"},
        "sources": [1],
        "tags": [f"signs{UNSTORABLE}"],
    }
    deck = {"title": f"Road{UNSTORABLE} signs", "description": f"Warning{UNSTORABLE} signs"}
    llm = FakeLlmClient(model_reply({"deck": deck, "notes": [note]}))
    providers = FakeProviders()
    run = RunGeneration(
        store=store,
        retriever=providers,
        parser=providers,
        generator=LlmCardGenerator(llm=llm, new_id=uuid4, handlers=NOTE_TYPE_HANDLERS),
        media=providers,
        clock=utc_now,
    )

    await run(job.job_id)

    stored = await store.get(job.job_id)
    assert stored is not None
    assert isinstance(stored.state, Succeeded)
    [stored_note] = stored.state.result.notes
    assert isinstance(stored_note.fields, BasicFields)
    stored_text = "".join(
        [stored_note.fields.front, stored_note.fields.back, *stored_note.tags, stored.state.result.deck.title]
    )
    assert "\x00" not in stored_text
    assert all(unicodedata.category(character) != SURROGATE_CATEGORY for character in stored_text)
