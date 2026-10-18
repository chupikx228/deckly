from dataclasses import replace
from pathlib import Path

import pytest

from deckly.application.ports import SourceMaterial
from deckly.domain.deck import GenerationResult
from deckly.domain.notes.note_type import NoteType
from deckly.domain.source import TranscriptSource
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.moderation.moderator import LlmContentModerator
from deckly.infrastructure.moderation.prompt import build_content_prompt, build_material_prompt
from deckly.infrastructure.transcription.client import BYTES_PER_SECOND, Transcript
from deckly.transport.results import GenerationResultBody
from tests.domain.builders import FULL_RESULT, JOB_ID, TRANSCRIPT_SOURCE
from tests.fake_process import fake_program
from tests.fakes import FakeLlmClient, generation_request, model_reply, sequential_job_ids
from tests.transcription import ScriptedTranscriptionClient, recording, segment, transcriber_with
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.anyio

LECTURE = recording(1, seconds=130.0)
FIRST = "A red triangle warns of a hazard ahead. It is placed well before the hazard itself."
SECOND = "A diamond marks a priority road. A white bar on red means no entry for any vehicle."
DECK = {"title": "Road signs", "description": "From the lecture", "tags": ["signs"]}


async def lecture_material(tmp_path: Path) -> tuple[SourceMaterial, ...]:
    ffmpeg = fake_program(tmp_path, "ffmpeg", {"stdout_bytes": 130 * BYTES_PER_SECOND})
    transcript = Transcript(segments=(segment(0, 61, FIRST), segment(61, 128, SECOND)), language="en")
    return await transcriber_with(ffmpeg, ScriptedTranscriptionClient(transcript=transcript)).parse(
        LECTURE, b"stored recording"
    )


def passage(start_ms: int, end_ms: int) -> TranscriptSource:
    return TranscriptSource(
        title=LECTURE.filename, document_id=LECTURE.document_id, start_ms=start_ms, end_ms=end_ms
    )


def note_citing(front: str, sources: list[int]) -> dict[str, object]:
    return {"noteType": NoteType.BASIC, "fields": {"front": front, "back": "Answer"}, "sources": sources}


async def generated_from_lecture(llm: FakeLlmClient, tmp_path: Path) -> GenerationResult:
    ids = sequential_job_ids()
    generator = LlmCardGenerator(llm=llm, new_id=lambda: next(ids), handlers=NOTE_TYPE_HANDLERS)
    cards = await generator.generate(JOB_ID, generation_request(), await lecture_material(tmp_path))
    return cards.result


async def test_card_generator_cites_transcript_passages_exactly_as_it_cites_pages(tmp_path: Path) -> None:
    llm = FakeLlmClient(
        model_reply({"deck": DECK, "notes": [note_citing("Triangle?", [1]), note_citing("Diamond?", [2, 1])]})
    )

    result = await generated_from_lecture(llm, tmp_path)

    first, second = passage(0, 61_000), passage(61_000, 128_000)
    assert [note.sources for note in result.notes] == [(first,), (second, first)]
    [prompt] = llm.prompts
    assert f'<source number="1">\nTitle: {LECTURE.filename}\n\n{FIRST}\n</source>' in prompt.user
    assert f'<source number="2">\nTitle: {LECTURE.filename}\n\n{SECOND}\n</source>' in prompt.user
    body = GenerationResultBody.from_result(result).model_dump(mode="json", by_alias=True)
    assert spec_errors("GenerationResult", body) == []
    assert body["notes"][0]["sources"] == [
        {
            "kind": "transcript",
            "title": LECTURE.filename,
            "documentId": str(LECTURE.document_id),
            "startMs": 0,
            "endMs": 61_000,
        }
    ]


def test_classifier_sees_the_same_prompt_whether_notes_cite_recordings_or_the_web() -> None:
    from_recordings = replace(
        FULL_RESULT, notes=tuple(replace(note, sources=(TRANSCRIPT_SOURCE,)) for note in FULL_RESULT.notes)
    )

    assert build_content_prompt(from_recordings) == build_content_prompt(FULL_RESULT)


async def test_moderator_screens_transcript_cited_notes_and_keeps_their_citations(tmp_path: Path) -> None:
    result = await generated_from_lecture(
        FakeLlmClient(model_reply({"deck": DECK, "notes": [note_citing("Triangle?", [1])]})), tmp_path
    )
    verdicts = model_reply({"deck": "allow", "notes": {"1": "allow"}})

    screened = await LlmContentModerator(llm=FakeLlmClient(verdicts)).screen(
        JOB_ID, generation_request().source_query().topic, result
    )

    assert screened == result
    assert screened.notes[0].sources == (passage(0, 61_000),)


async def test_transcript_is_screened_before_generation_as_numbered_units_like_pages(tmp_path: Path) -> None:
    material = await lecture_material(tmp_path)

    prompt = build_material_prompt(LECTURE, material)

    assert LECTURE.filename in prompt.user
    assert FIRST in prompt.user
    assert SECOND in prompt.user
