from collections.abc import Callable
from uuid import uuid4

import httpx2
import pytest

from deckly.application.pipeline import RunGeneration
from deckly.domain.job import Failed, FailureCode, GenerationJob, Succeeded
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import Source
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.llm.client import LlmReply
from tests.domain.builders import T0, at
from tests.fakes import (
    FakeLlmClient,
    FakeProviders,
    InMemoryJobStore,
    ManualTime,
    generation_request,
    model_reply,
    scope,
    tavily_result,
    web_sources,
)

pytestmark = pytest.mark.anyio

type Handler = Callable[[httpx2.Request], httpx2.Response]

RETRIEVED_AT = at(5)
PAGE_TEXT = "A red triangle warns of danger ahead. A blue circle gives a mandatory instruction to drivers."
OTHER_TEXT = "An octagon means stop. A yellow diamond marks a priority road for everyone driving on it."
BINARY = "PK\x03\x04\x00\x00\ufffd\ufffd\x00\ufffd" * 100


def note(front: str, sources: object) -> dict[str, object]:
    return {"noteType": NoteType.BASIC, "fields": {"front": front, "back": "Answer"}, "sources": sources}


def reply(*notes: dict[str, object]) -> LlmReply:
    return model_reply({"deck": {"title": "Road signs"}, "notes": list(notes)})


def answering(*results: dict[str, object]) -> Handler:
    return lambda _: httpx2.Response(200, json={"query": "Road signs", "results": list(results)})


class Pipeline:
    def __init__(self, handler: Handler, llm: FakeLlmClient) -> None:
        self.store = InMemoryJobStore()
        self.time = ManualTime()
        self.llm = llm
        retriever, parser = web_sources(handler, self.time, lambda: RETRIEVED_AT)
        self.retriever = retriever
        providers = FakeProviders()
        self.run = RunGeneration(
            store=self.store,
            retriever=self.retriever,
            parser=parser,
            generator=LlmCardGenerator(llm=llm, new_id=uuid4, handlers=NOTE_TYPE_HANDLERS),
            media=providers,
            clock=lambda: T0,
        )

    async def finish(self) -> GenerationJob:
        job = GenerationJob.queue(uuid4(), T0)
        await self.store.add(job, generation_request(), scope())
        try:
            await self.run(job.job_id)
        finally:
            await self.retriever.aclose()
        finished = await self.store.get(job.job_id)
        assert finished is not None
        return finished


async def test_notes_carry_sources_that_trace_back_to_the_fetched_pages() -> None:
    handler = answering(
        tavily_result("Warning signs", "https://example.com/warning", PAGE_TEXT),
        tavily_result("Priority signs", "https://example.org/priority", OTHER_TEXT),
    )
    llm = FakeLlmClient(reply(note("Red triangle?", [1]), note("Octagon?", [2, 1])))

    job = await Pipeline(handler, llm).finish()

    assert isinstance(job.state, Succeeded)
    warning = Source(title="Warning signs", url="https://example.com/warning", retrieved_at=RETRIEVED_AT)
    priority = Source(title="Priority signs", url="https://example.org/priority", retrieved_at=RETRIEVED_AT)
    assert [n.sources for n in job.state.result.notes] == [(warning,), (priority, warning)]
    prompt = llm.prompts[0].user
    assert PAGE_TEXT in prompt
    assert OTHER_TEXT in prompt


async def test_citation_numbers_follow_the_material_that_survived_parsing_not_the_raw_results() -> None:
    handler = answering(
        tavily_result("Binary download", "https://example.com/file.zip", BINARY),
        tavily_result("No page text", "https://example.com/empty", None),
        tavily_result("Warning signs", "https://example.com/warning", PAGE_TEXT),
    )
    llm = FakeLlmClient(reply(note("Red triangle?", [1])))

    job = await Pipeline(handler, llm).finish()

    assert isinstance(job.state, Succeeded)
    [kept] = job.state.result.notes
    assert [source.url for source in kept.sources] == ["https://example.com/warning"]
    assert "file.zip" not in llm.prompts[0].user


async def test_citation_of_material_that_does_not_exist_drops_the_note_not_the_job() -> None:
    handler = answering(tavily_result("Warning signs", "https://example.com/warning", PAGE_TEXT))
    llm = FakeLlmClient(reply(note("Real?", [1]), note("Invented?", [2]), note("Uncited?", [])))

    job = await Pipeline(handler, llm).finish()

    assert isinstance(job.state, Succeeded)
    assert [n.sources[0].url for n in job.state.result.notes] == ["https://example.com/warning"]


async def test_search_timeout_fails_the_job_as_provider_unavailable() -> None:
    def timing_out(request: httpx2.Request) -> httpx2.Response:
        message = "search took too long"
        raise httpx2.ReadTimeout(message, request=request)

    llm = FakeLlmClient(reply(note("Never asked?", [1])))

    job = await Pipeline(timing_out, llm).finish()

    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.PROVIDER_UNAVAILABLE
    assert llm.prompts == []


async def test_search_outage_fails_the_job_as_provider_unavailable() -> None:
    llm = FakeLlmClient(reply(note("Never asked?", [1])))

    job = await Pipeline(lambda _: httpx2.Response(503, json={}), llm).finish()

    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.PROVIDER_UNAVAILABLE


async def test_exhausted_search_quota_fails_the_job_as_provider_unavailable() -> None:
    llm = FakeLlmClient(reply(note("Never asked?", [1])))

    job = await Pipeline(lambda _: httpx2.Response(432, json={}), llm).finish()

    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.PROVIDER_UNAVAILABLE


async def test_rejected_search_request_fails_the_job_without_blaming_the_provider() -> None:
    llm = FakeLlmClient(reply(note("Never asked?", [1])))

    job = await Pipeline(lambda _: httpx2.Response(401, json={}), llm).finish()

    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.GENERATION_FAILED


async def test_search_with_no_results_fails_the_job_as_no_valid_content_without_calling_the_model() -> None:
    llm = FakeLlmClient(reply(note("Never asked?", [1])))

    job = await Pipeline(answering(), llm).finish()

    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.NO_VALID_CONTENT
    assert llm.prompts == []


async def test_every_page_unusable_fails_the_job_as_no_valid_content_without_calling_the_model() -> None:
    handler = answering(
        tavily_result("Binary", "https://example.com/file.zip", BINARY),
        tavily_result("Short", "https://example.com/short", "Stop."),
        tavily_result("   ", "https://example.com/untitled", PAGE_TEXT),
        tavily_result("Bad url", "ftp://example.com/signs", PAGE_TEXT),
    )
    llm = FakeLlmClient(reply(note("Never asked?", [1])))

    job = await Pipeline(handler, llm).finish()

    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.NO_VALID_CONTENT
    assert llm.prompts == []
