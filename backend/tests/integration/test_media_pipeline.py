from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.pipeline import RunGeneration
from deckly.domain.job import GenerationJob, Succeeded
from deckly.domain.media import MediaKind
from deckly.domain.notes.basic import BasicFields
from deckly.domain.notes.note_type import NoteType
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.clock import utc_now
from deckly.infrastructure.job_store import PostgresJobStore
from deckly.transport.results import GenerationResultBody
from tests.domain.builders import T0
from tests.fakes import (
    FakeLlmClient,
    FakeProviders,
    InMemoryResultCache,
    ManualTime,
    commons_fetcher,
    commons_page,
    commons_results,
    generation_request,
    model_reply,
)
from tests.integration.conftest import Cleanup
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOTES = [
    {
        "noteType": NoteType.BASIC,
        "fields": {"front": "Что означает красный восьмиугольник?", "back": "Стоп"},
        "sources": [1],
        "image": "stop sign",
    },
    {
        "noteType": NoteType.BASIC,
        "fields": {"front": "Что означает перевёрнутый треугольник?", "back": "Уступите дорогу"},
        "sources": [1],
        "image": "yield sign",
    },
    {
        "noteType": NoteType.BASIC,
        "fields": {"front": "Кто регулирует движение?", "back": "Регулировщик"},
        "sources": [1],
    },
]
LICENSED = commons_page(1, "File:Stop sign.svg", license_code="pd", description="Red <b>STOP</b> sign")
UNLICENSED = commons_page(1, "File:Yield.jpg", license_code="cc-by-sa-4.0", attribution_required="true")


async def run_with(
    store: PostgresJobStore, cleanup: Cleanup, handler: Callable[[httpx2.Request], httpx2.Response]
) -> GenerationJob:
    providers = FakeProviders()
    media = commons_fetcher(handler, ManualTime())
    run = RunGeneration(
        store=store,
        cache=InMemoryResultCache(),
        retriever=providers,
        parser=providers,
        generator=LlmCardGenerator(
            llm=FakeLlmClient(model_reply({"deck": {"title": "Знаки"}, "notes": NOTES})),
            new_id=uuid4,
            handlers=NOTE_TYPE_HANDLERS,
        ),
        moderator=providers,
        media=media,
        clock=utc_now,
    )
    job = GenerationJob.queue(uuid4(), T0)
    await store.add(job, replace(generation_request(), include_images=True), cleanup.scope())
    try:
        await run(job.job_id)
    finally:
        await media.aclose()
    stored = await store.get(job.job_id)
    assert stored is not None
    return stored


def fronts_with_images(job: GenerationJob) -> dict[str, int]:
    assert isinstance(job.state, Succeeded)
    return {
        note.fields.front: len(note.media)
        for note in job.state.result.notes
        if isinstance(note.fields, BasicFields)
    }


async def test_only_images_with_a_derivable_licence_are_stored_against_the_notes_the_model_illustrated(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    searched: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        terms = request.url.params["gsrsearch"]
        searched.append(terms)
        page = LICENSED if terms.startswith("stop sign") else UNLICENSED
        return httpx2.Response(200, json=commons_results(page))

    stored = await run_with(PostgresJobStore(session_factory), cleanup, handler)

    assert sorted(searched) == ["stop sign filetype:bitmap|drawing", "yield sign filetype:bitmap|drawing"]
    assert fronts_with_images(stored) == {
        "Что означает красный восьмиугольник?": 1,
        "Что означает перевёрнутый треугольник?": 0,
        "Кто регулирует движение?": 0,
    }
    assert isinstance(stored.state, Succeeded)
    [image] = [item for note in stored.state.result.notes for item in note.media]
    assert (image.kind, image.license, image.alt) == (MediaKind.IMAGE, "Public-Domain", "Red STOP sign")
    assert image.url.startswith("https://upload.wikimedia.org/")
    body = GenerationResultBody.from_result(stored.state.result).model_dump(mode="json", by_alias=True)
    assert spec_errors("GenerationResult", body) == []


async def test_image_provider_outage_is_stored_as_a_succeeded_job_with_an_image_free_deck(
    session_factory: async_sessionmaker[AsyncSession], cleanup: Cleanup
) -> None:
    stored = await run_with(PostgresJobStore(session_factory), cleanup, lambda _: httpx2.Response(503))

    assert fronts_with_images(stored) == {
        "Что означает красный восьмиугольник?": 0,
        "Что означает перевёрнутый треугольник?": 0,
        "Кто регулирует движение?": 0,
    }
