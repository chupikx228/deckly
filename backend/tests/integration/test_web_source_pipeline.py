from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.pipeline import RunGeneration
from deckly.domain.job import Failed, FailureCode, GenerationJob, Succeeded
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source
from deckly.domain.text import is_web_url
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.job_store import PostgresJobStore
from tests.domain.builders import MOSCOW, T0
from tests.fakes import (
    FakeLlmClient,
    FakeProviders,
    ManualTime,
    generation_request,
    model_reply,
    tavily_result,
    web_sources,
)
from tests.integration.conftest import Cleanup

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

RETRIEVED_AT = datetime(2026, 8, 14, 13, 45, 12, 345678, tzinfo=MOSCOW)
PAGE_TEXT = (
    "Дорожный знак «Уступите дорогу» — перевёрнутый треугольник, окаймлённый красной полосой, на белом поле."
)
OTHER_TEXT = "A red octagon with the word STOP means come to a complete stop before the line."


async def run_with(
    store: PostgresJobStore,
    cleanup: Cleanup,
    handler: Callable[[httpx2.Request], httpx2.Response],
    llm: FakeLlmClient,
) -> GenerationJob:
    retriever, parser = web_sources(handler, ManualTime(), lambda: RETRIEVED_AT)
    providers = FakeProviders()
    run = RunGeneration(
        store=store,
        retriever=retriever,
        parser=parser,
        generator=LlmCardGenerator(llm=llm, new_id=uuid4, handlers=NOTE_TYPE_HANDLERS),
        media=providers,
        clock=utc_now,
    )
    job = GenerationJob.queue(uuid4(), T0)
    await store.add(job, generation_request(), cleanup.scope())
    try:
        await run(job.job_id)
    finally:
        await retriever.aclose()
    stored = await store.get(job.job_id)
    assert stored is not None
    return stored


async def test_stored_notes_keep_well_formed_sources_that_trace_back_to_the_fetched_pages(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    results = [
        tavily_result(
            "Знаки приоритета\x00 —\u200b Википедия", "https://ru.wikipedia.org/wiki/Знаки", PAGE_TEXT
        ),
        tavily_result("Stop sign", "https://example.com/stop?lang=en#top", OTHER_TEXT),
    ]
    note = {
        "noteType": NoteType.BASIC,
        "fields": {"front": "Уступите дорогу?", "back": "Перевёрнутый треугольник"},
        "sources": [1, 2],
    }
    llm = FakeLlmClient(model_reply({"deck": {"title": "Знаки"}, "notes": [note]}))

    stored = await run_with(
        PostgresJobStore(session_factory),
        cleanup,
        lambda _: httpx2.Response(200, json={"results": results}),
        llm,
    )

    assert isinstance(stored.state, Succeeded)
    [stored_note] = stored.state.result.notes
    assert stored_note.sources == (
        Source(
            title="Знаки приоритета — Википедия",
            url="https://ru.wikipedia.org/wiki/Знаки",
            retrieved_at=RETRIEVED_AT.astimezone(UTC),
        ),
        Source(
            title="Stop sign",
            url="https://example.com/stop?lang=en#top",
            retrieved_at=RETRIEVED_AT.astimezone(UTC),
        ),
    )
    for source in stored_note.sources:
        assert is_web_url(source.url)
        assert source.retrieved_at == RETRIEVED_AT


async def test_search_timeout_is_stored_as_provider_unavailable(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    def timing_out(request: httpx2.Request) -> httpx2.Response:
        message = "search took too long"
        raise httpx2.ReadTimeout(message, request=request)

    llm = FakeLlmClient(model_reply({"notes": []}))

    stored = await run_with(PostgresJobStore(session_factory), cleanup, timing_out, llm)

    assert isinstance(stored.state, Failed)
    assert stored.state.code is FailureCode.PROVIDER_UNAVAILABLE
    assert llm.prompts == []
