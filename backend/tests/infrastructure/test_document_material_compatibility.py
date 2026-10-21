from dataclasses import replace

import pytest

from deckly.application.ports import SourceMaterial
from deckly.domain.deck import GenerationResult
from deckly.domain.document import DocumentFormat
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import CitationUnit, DocumentSource, WebSource
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.documents.parser import DocumentTextParser
from deckly.infrastructure.moderation.moderator import LlmContentModerator
from deckly.infrastructure.moderation.prompt import build_content_prompt
from deckly.transport.results import GenerationResultBody
from tests.document_files import TOPIC_TEXT, text_pdf
from tests.documents import document
from tests.domain.builders import DOCUMENT_SOURCE, FULL_RESULT, JOB_ID
from tests.fakes import FakeLlmClient, generation_request, model_reply, sequential_job_ids
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.anyio

LECTURE = document(1)
SECOND_TEXT = "A diamond marks a priority road. A white bar on red means no entry for any vehicle."
DECK = {"title": "Road signs", "description": "From the lecture", "tags": ["signs"]}


async def lecture_material() -> tuple[SourceMaterial, ...]:
    parser = DocumentTextParser(max_characters=4000, deadline_seconds=30)
    return await parser.parse(LECTURE, text_pdf(TOPIC_TEXT, None, SECOND_TEXT))


def note_citing(front: str, sources: list[int]) -> dict[str, object]:
    return {"noteType": NoteType.BASIC, "fields": {"front": front, "back": "Answer"}, "sources": sources}


async def generated_from_lecture(llm: FakeLlmClient) -> GenerationResult:
    ids = sequential_job_ids()
    generator = LlmCardGenerator(llm=llm, new_id=lambda: next(ids), handlers=NOTE_TYPE_HANDLERS)
    cards = await generator.generate(JOB_ID, generation_request(), await lecture_material())
    return cards.result


def page(number: int) -> DocumentSource:
    return DocumentSource(
        title=LECTURE.filename, document_id=LECTURE.document_id, unit=CitationUnit.PAGE, number=number
    )


async def test_card_generator_cites_document_pages_exactly_as_it_cites_web_pages() -> None:
    llm = FakeLlmClient(
        model_reply({"deck": DECK, "notes": [note_citing("Triangle?", [1]), note_citing("Diamond?", [2, 1])]})
    )

    result = await generated_from_lecture(llm)

    assert [note.sources for note in result.notes] == [(page(1),), (page(3), page(1))]
    [prompt] = llm.prompts
    assert f'<source number="1">\nTitle: {LECTURE.filename}\n\n{TOPIC_TEXT}\n</source>' in prompt.user
    assert f'<source number="2">\nTitle: {LECTURE.filename}\n\n{SECOND_TEXT}\n</source>' in prompt.user
    body = GenerationResultBody.from_result(result).model_dump(mode="json", by_alias=True)
    assert spec_errors("GenerationResult", body) == []


def test_classifier_sees_the_same_prompt_whether_notes_cite_documents_or_the_web() -> None:
    from_documents = replace(
        FULL_RESULT, notes=tuple(replace(note, sources=(DOCUMENT_SOURCE,)) for note in FULL_RESULT.notes)
    )

    assert build_content_prompt(from_documents) == build_content_prompt(FULL_RESULT)


async def test_moderator_screens_document_cited_notes_and_keeps_their_citations() -> None:
    result = await generated_from_lecture(
        FakeLlmClient(model_reply({"deck": DECK, "notes": [note_citing("Triangle?", [1])]}))
    )
    verdicts = model_reply({"deck": "allow", "notes": {"1": "allow"}})

    screened = await LlmContentModerator(llm=FakeLlmClient(verdicts)).screen(
        JOB_ID, generation_request().source_query().topic, result
    )

    assert screened == result
    assert screened.notes[0].sources == (page(1),)


PAGE_URL = "https://en.wikipedia.org/wiki/Road_signs"
FETCHED_PAGE = replace(
    document(2), filename="Road signs - Wikipedia", document_format=DocumentFormat.TEXT, source_url=PAGE_URL
)


async def test_card_generator_cites_a_fetched_page_as_the_web_source_it_was_read_from() -> None:
    parser = DocumentTextParser(max_characters=4000, deadline_seconds=30)
    material = await parser.parse(FETCHED_PAGE, f"{TOPIC_TEXT}\n{SECOND_TEXT}".encode())
    llm = FakeLlmClient(model_reply({"deck": DECK, "notes": [note_citing("Triangle?", [1])]}))
    ids = sequential_job_ids()
    generator = LlmCardGenerator(llm=llm, new_id=lambda: next(ids), handlers=NOTE_TYPE_HANDLERS)

    result = (await generator.generate(JOB_ID, generation_request(), material)).result

    cited = WebSource(title=FETCHED_PAGE.filename, url=PAGE_URL, retrieved_at=FETCHED_PAGE.uploaded_at)
    assert [note.sources for note in result.notes] == [(cited,)]
    body = GenerationResultBody.from_result(result).model_dump(mode="json", by_alias=True)
    assert spec_errors("GenerationResult", body) == []
    assert body["notes"][0]["sources"] == [
        {
            "kind": "web",
            "title": FETCHED_PAGE.filename,
            "url": PAGE_URL,
            "retrievedAt": "2026-08-14T10:30:00Z",
        }
    ]
