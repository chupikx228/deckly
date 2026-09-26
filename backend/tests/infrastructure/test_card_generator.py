import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable

import pytest

from deckly.application.exceptions import UpstreamUnavailableError
from deckly.application.pipeline import RunGeneration
from deckly.application.ports import SourceMaterial
from deckly.domain.deck import MAX_DESCRIPTION_LENGTH, MAX_TITLE_LENGTH, GenerationResult
from deckly.domain.generation import Difficulty, GenerationRequest
from deckly.domain.job import Cancelled, Failed, FailureCode, GenerationJob, JobStage, Succeeded
from deckly.domain.notes.basic import BasicFields
from deckly.domain.notes.cloze import ClozeFields, cloze_numbers
from deckly.domain.notes.multiple_choice import MultipleChoiceFields
from deckly.domain.notes.note_type import NoteType
from deckly.domain.notes.registry import NOTE_FIELDS_BY_TYPE
from deckly.domain.source import Source
from deckly.domain.text import is_blank, utf16_length
from deckly.infrastructure.card_generator.generator import LlmCardGenerator
from deckly.infrastructure.card_generator.note_types import NOTE_TYPE_HANDLERS
from deckly.infrastructure.llm.client import (
    LlmClient,
    LlmRejectedError,
    LlmReply,
    LlmResponseError,
    LlmStop,
    LlmUnavailableError,
)
from deckly.infrastructure.llm.resilient import ResilientLlmClient
from deckly.infrastructure.resilience import (
    CircuitBreaker,
    CircuitState,
    ResilientCaller,
    RetryPolicy,
    RetryRuntime,
)
from deckly.transport.results import GenerationResultBody
from tests.domain.builders import JOB_ID, T0
from tests.fakes import (
    MODEL_THINKING_SECONDS,
    FakeLlmClient,
    Harness,
    LlmOutcome,
    SlowModel,
    generation_request,
    hang_forever,
    job_id,
    model_reply,
    scope,
    sequential_job_ids,
)
from tests.transport.openapi import spec_errors

pytestmark = pytest.mark.anyio

TOPIC = "Road signs"
GENERATOR_LOGGER = "deckly.infrastructure.card_generator.generator"
HANDLED_TYPES = tuple(NOTE_TYPE_HANDLERS)
MATERIAL = (
    SourceMaterial(
        source=Source(title="Traffic regulations", url="https://example.com/rules", retrieved_at=T0),
        text="A red triangle warns of a hazard ahead. A red circle prohibits.",
    ),
    SourceMaterial(
        source=Source(title="Road sign catalogue", url="https://example.com/signs"),
        text="The stop sign is a red octagon.",
    ),
)
FIRST_SOURCE = (1,)
DECK = {"title": "Signs of the road", "description": "Warning and prohibitory signs", "tags": ["driving"]}
VALID_FIELDS: dict[NoteType, dict[str, object]] = {
    NoteType.BASIC: {"front": "What does a red triangle warn of?", "back": "A hazard ahead"},
    NoteType.BASIC_REVERSED: {"front": "Stop sign", "back": "Red octagon"},
    NoteType.BASIC_TYPE_IN: {"front": "Shape of a stop sign", "back": "Octagon"},
    NoteType.BASIC_OPTIONAL_REVERSED: {"front": "Red circle", "back": "Prohibition", "addReverse": True},
    NoteType.CLOZE: {"text": "A {{c1::red triangle}} warns of a {{c2::hazard}}", "extra": "Regulations"},
    NoteType.MULTIPLE_CHOICE: {
        "question": "Which shape is a stop sign?",
        "answer": "Octagon",
        "distractors": ["Circle", "Triangle"],
    },
    NoteType.IMAGE_OCCLUSION: {
        "imageId": "3d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a",
        "regions": [{"ordinal": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}],
    },
}


def request(*note_types: NoteType, card_count: int = 10) -> GenerationRequest:
    return GenerationRequest(
        topic=TOPIC,
        language="en",
        card_count=card_count,
        difficulty=Difficulty.INTERMEDIATE,
        note_types=tuple(dict.fromkeys(note_types)),
        include_images=NoteType.IMAGE_OCCLUSION in note_types,
        instructions=None,
    )


def note_json(note_type: str, fields: object, *, sources: object = FIRST_SOURCE) -> dict[str, object]:
    return {"noteType": note_type, "fields": fields, "sources": sources}


def valid_note(note_type: NoteType) -> dict[str, object]:
    return note_json(note_type, VALID_FIELDS[note_type])


def with_fields(note_type: NoteType, **changes: object) -> dict[str, object]:
    return note_json(note_type, {**VALID_FIELDS[note_type], **changes})


def basic(number: int) -> dict[str, object]:
    return note_json(NoteType.BASIC, {"front": f"front {number}", "back": f"back {number}"})


def document(*notes: object, deck: object = DECK) -> dict[str, object]:
    return {"deck": deck, "notes": list(notes)}


async def generate(
    llm: LlmClient, job_request: GenerationRequest, material: tuple[SourceMaterial, ...] = MATERIAL
) -> GenerationResult:
    ids = sequential_job_ids()
    generator = LlmCardGenerator(llm=llm, new_id=lambda: next(ids), handlers=NOTE_TYPE_HANDLERS)
    return await generator.generate(JOB_ID, job_request, material)


def assert_valid_for(result: GenerationResult, job_request: GenerationRequest) -> None:
    cited = {entry.source for entry in MATERIAL}
    assert len(result.notes) <= job_request.card_count
    for note in result.notes:
        assert note.note_type in job_request.note_types
        assert type(note.fields) is NOTE_FIELDS_BY_TYPE[note.note_type]
        assert note.sources
        assert set(note.sources) <= cited
    body = GenerationResultBody.from_result(result).model_dump(mode="json", by_alias=True)
    assert spec_errors("GenerationResult", body) == []


def fronts(result: GenerationResult) -> list[str]:
    return [note.fields.front for note in result.notes if isinstance(note.fields, BasicFields)]


async def test_valid_notes_of_every_handled_type_come_back_as_domain_notes_citing_the_material() -> None:
    job_request = request(*HANDLED_TYPES)
    llm = FakeLlmClient(model_reply(document(*(valid_note(note_type) for note_type in HANDLED_TYPES))))

    result = await generate(llm, job_request)

    assert [note.note_type for note in result.notes] == list(HANDLED_TYPES)
    assert all(note.sources == (MATERIAL[0].source,) for note in result.notes)
    assert len({note.client_id for note in result.notes}) == len(result.notes)
    assert_valid_for(result, job_request)
    assert len(llm.prompts) == 1


WRAPPED_REPLIES = {
    "json code fence with prose around it": "Here are your cards:\n```json\nDOCUMENT\n```\nEnjoy!",
    "bare code fence": "```\nDOCUMENT\n```",
    "prose before and after": "Sure! DOCUMENT Let me know if you need more.",
    "cloze markers in the prose before the json": "I used {{c1::cloze}} markers where useful:\nDOCUMENT",
    "a stray brace in the prose before the json": "Notes { as requested }:\nDOCUMENT",
    "a second json object after the first": 'DOCUMENT\n{"notes": []}',
}


@pytest.mark.parametrize("template", WRAPPED_REPLIES.values(), ids=WRAPPED_REPLIES.keys())
async def test_json_wrapped_in_prose_or_code_fences_is_extracted(template: str) -> None:
    text = template.replace("DOCUMENT", json.dumps(document(basic(1), basic(2))))

    result = await generate(
        FakeLlmClient(LlmReply(text=text, stop=LlmStop.COMPLETE)), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front 1", "front 2"]
    assert result.deck.title == DECK["title"]


DECK_ONLY = json.dumps({"deck": DECK})
NOTES_ONLY = json.dumps({"notes": [basic(1), basic(2)]})
SPLIT_REPLIES = {
    "deck object then notes object": f"{DECK_ONLY}\n{NOTES_ONLY}",
    "notes object then deck object": f"{NOTES_ONLY}\n{DECK_ONLY}",
    "each object in its own code fence with prose between": (
        f"```json\n{DECK_ONLY}\n```\nAnd the notes:\n```json\n{NOTES_ONLY}\n```"
    ),
    "notes continued in a second object": json.dumps(document(basic(1))) + json.dumps({"notes": [basic(2)]}),
    "a later deck does not replace the first": (
        json.dumps(document(basic(1))) + json.dumps(document(basic(2), deck={"title": "Replacement"}))
    ),
    "a document-shaped value nested in a note is not merged": json.dumps(
        document({**basic(1), "context": {"notes": [basic(9)]}}, basic(2))
    ),
}


@pytest.mark.parametrize("text", SPLIT_REPLIES.values(), ids=SPLIT_REPLIES.keys())
async def test_deck_and_notes_are_merged_from_every_top_level_object_of_the_reply(text: str) -> None:
    result = await generate(
        FakeLlmClient(LlmReply(text=text, stop=LlmStop.COMPLETE)), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front 1", "front 2"]
    assert result.deck.title == DECK["title"]


async def test_second_object_cut_off_mid_note_keeps_its_complete_notes_and_the_first_object() -> None:
    full = f"{DECK_ONLY}\n{json.dumps({'notes': [basic(1), basic(2), basic(3)]})}"
    cut = full[: full.index("front 3")]

    result = await generate(
        FakeLlmClient(LlmReply(text=cut, stop=LlmStop.TRUNCATED)), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front 1", "front 2"]
    assert result.deck.title == DECK["title"]


async def test_reply_whose_later_object_had_to_be_salvaged_is_logged_as_incomplete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = json.dumps({"notes": [basic(1), basic(2)]}).replace('"front 2"', '"front "2"', 1)
    llm = FakeLlmClient(LlmReply(text=f"{DECK_ONLY}\n{broken}", stop=LlmStop.COMPLETE))

    with caplog.at_level(logging.INFO, logger=GENERATOR_LOGGER):
        await generate(llm, request(NoteType.BASIC))

    [record] = [record for record in caplog.records if record.getMessage() == "card_generation_finished"]
    assert (record.levelno, record.__dict__["document_complete"]) == (logging.WARNING, False)


async def test_top_level_list_of_notes_is_accepted_and_the_deck_falls_back_to_the_topic() -> None:
    result = await generate(FakeLlmClient(model_reply([basic(1)])), request(NoteType.BASIC))

    assert fronts(result) == ["front 1"]
    assert result.deck.title == TOPIC


UNUSABLE_REPLIES = {
    "empty": "",
    "whitespace": " \n\t ",
    "prose only": "I cannot help with that.",
    "json null": "null",
    "json number": "42",
    "json string": '"notes"',
    "empty object": "{}",
    "notes is null": '{"notes": null}',
    "notes is an object": '{"notes": {"noteType": "basic"}}',
    "notes that are not objects": '{"notes": [1, "basic", null, [], true]}',
    "unquoted keys": "{notes: [{noteType: basic}]}",
    "single quotes": "{'notes': [{'noteType': 'basic'}]}",
    "deeply nested arrays": "[" * 100_000,
    "deeply nested notes": '{"notes": ' + "[" * 100_000,
    "integer too long to convert": '{"notes": [' + "9" * 5000 + "]}",
    "unbalanced braces": "}{" * 1000,
    "control characters and a lone surrogate": "\x00\x01\x02\ufffd\ud800",
}


@pytest.mark.parametrize("text", UNUSABLE_REPLIES.values(), ids=UNUSABLE_REPLIES.keys())
async def test_unusable_reply_yields_no_notes_without_raising_or_retrying(text: str) -> None:
    llm = FakeLlmClient(LlmReply(text=text, stop=LlmStop.COMPLETE))

    result = await generate(llm, request(NoteType.BASIC))

    assert result.notes == ()
    assert result.deck.title == TOPIC
    assert len(llm.prompts) == 1


async def test_adversarially_nested_reply_is_abandoned_in_linear_time() -> None:
    unit = '{"noteType": "basic", "fields": {"front": '
    text = '{"notes": [' + unit * (1_000_000 // len(unit))
    llm = FakeLlmClient(LlmReply(text=text, stop=LlmStop.TRUNCATED))
    started = time.perf_counter()

    result = await generate(llm, request(NoteType.BASIC))

    assert time.perf_counter() - started < 1.0
    assert result.notes == ()


async def test_reply_cut_off_mid_note_keeps_every_complete_note_before_the_cut() -> None:
    full = json.dumps(document(basic(1), basic(2), basic(3)))
    cut = full[: full.index("front 3")]

    result = await generate(
        FakeLlmClient(LlmReply(text=cut, stop=LlmStop.TRUNCATED)), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front 1", "front 2"]
    assert result.deck.title == DECK["title"]


async def test_reply_cut_off_inside_the_deck_keeps_no_notes_and_falls_back_to_the_topic() -> None:
    full = json.dumps(document(basic(1)))
    cut = full[: full.index("Warning")]

    result = await generate(
        FakeLlmClient(LlmReply(text=cut, stop=LlmStop.TRUNCATED)), request(NoteType.BASIC)
    )

    assert result.notes == ()
    assert result.deck.title == TOPIC


async def test_one_syntactically_broken_note_is_skipped_and_the_notes_after_it_are_kept() -> None:
    text = json.dumps(document(basic(1), basic(2), basic(3))).replace('"front 2"', '"front "2"', 1)

    result = await generate(
        FakeLlmClient(LlmReply(text=text, stop=LlmStop.COMPLETE)), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front 1", "front 3"]


async def test_raw_newlines_inside_json_strings_are_accepted() -> None:
    text = json.dumps(document(basic(1))).replace("back 1", "back\n1")

    result = await generate(
        FakeLlmClient(LlmReply(text=text, stop=LlmStop.COMPLETE)), request(NoteType.BASIC)
    )

    assert len(result.notes) == 1


MISMATCHED_FIELDS: dict[str, tuple[NoteType, object]] = {
    "basic with question and answer": (NoteType.BASIC, {"question": "q", "answer": "a"}),
    "basic missing its back": (NoteType.BASIC, {"front": "f"}),
    "basic with an extra key": (NoteType.BASIC, {"front": "f", "back": "b", "hint": "h"}),
    "basic with a numeric back": (NoteType.BASIC, {"front": "f", "back": 60}),
    "basic with a null back": (NoteType.BASIC, {"front": "f", "back": None}),
    "basic fields as a list": (NoteType.BASIC, ["front", "back"]),
    "basic fields as a string": (NoteType.BASIC, "front: f, back: b"),
    "basic without fields": (NoteType.BASIC, None),
    "basic_reversed with cloze fields": (NoteType.BASIC_REVERSED, {"text": "{{c1::Stop}}"}),
    "basic_type_in with a list back": (NoteType.BASIC_TYPE_IN, {"front": "f", "back": ["60"]}),
    "optional reversed without addReverse": (NoteType.BASIC_OPTIONAL_REVERSED, {"front": "f", "back": "b"}),
    "optional reversed with addReverse as a string": (
        NoteType.BASIC_OPTIONAL_REVERSED,
        {"front": "f", "back": "b", "addReverse": "true"},
    ),
    "optional reversed with addReverse as a number": (
        NoteType.BASIC_OPTIONAL_REVERSED,
        {"front": "f", "back": "b", "addReverse": 1},
    ),
    "optional reversed with snake_case add_reverse": (
        NoteType.BASIC_OPTIONAL_REVERSED,
        {"front": "f", "back": "b", "add_reverse": True},
    ),
    "cloze with front and back": (NoteType.CLOZE, {"front": "f", "back": "b"}),
    "cloze with text as a list": (NoteType.CLOZE, {"text": ["{{c1::red}}"]}),
    "cloze with a numeric extra": (NoteType.CLOZE, {"text": "{{c1::red}}", "extra": 3}),
    "multiple choice with distractors as a string": (
        NoteType.MULTIPLE_CHOICE,
        {"question": "q", "answer": "a", "distractors": "b, c"},
    ),
    "multiple choice with a numeric distractor": (
        NoteType.MULTIPLE_CHOICE,
        {"question": "q", "answer": "a", "distractors": ["b", 2]},
    ),
    "multiple choice with front and back": (NoteType.MULTIPLE_CHOICE, {"front": "f", "back": "b"}),
}


@pytest.mark.parametrize(("note_type", "fields"), MISMATCHED_FIELDS.values(), ids=MISMATCHED_FIELDS.keys())
async def test_note_whose_fields_do_not_match_its_type_never_reaches_the_result(
    note_type: NoteType, fields: object
) -> None:
    job_request = request(note_type, NoteType.BASIC)
    llm = FakeLlmClient(model_reply(document(note_json(note_type, fields), basic(1))))

    result = await generate(llm, job_request)

    assert fronts(result) == ["front 1"]
    assert_valid_for(result, job_request)


MALFORMED_NOTES: dict[str, object] = {
    "a string": "basic",
    "a list": [NoteType.BASIC, VALID_FIELDS[NoteType.BASIC]],
    "no noteType": {"fields": VALID_FIELDS[NoteType.BASIC], "sources": [1]},
    "noteType that is not a string": {"noteType": 1, "fields": VALID_FIELDS[NoteType.BASIC], "sources": [1]},
    "unknown noteType": note_json("flashcard", VALID_FIELDS[NoteType.BASIC]),
    "noteType in another case": note_json("Basic", VALID_FIELDS[NoteType.BASIC]),
}


@pytest.mark.parametrize("malformed", MALFORMED_NOTES.values(), ids=MALFORMED_NOTES.keys())
async def test_malformed_note_is_dropped_without_affecting_the_others(malformed: object) -> None:
    result = await generate(
        FakeLlmClient(model_reply(document(malformed, basic(1)))), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front 1"]


@pytest.mark.parametrize("requested", HANDLED_TYPES)
async def test_note_of_a_type_the_request_did_not_ask_for_never_reaches_the_result(
    requested: NoteType,
) -> None:
    job_request = request(requested)
    llm = FakeLlmClient(model_reply(document(*(valid_note(note_type) for note_type in NoteType))))

    result = await generate(llm, job_request)

    assert [note.note_type for note in result.notes] == [requested]
    assert_valid_for(result, job_request)


async def test_image_occlusion_is_neither_offered_to_the_model_nor_accepted_from_it() -> None:
    job_request = request(NoteType.IMAGE_OCCLUSION, NoteType.BASIC)
    llm = FakeLlmClient(model_reply(document(valid_note(NoteType.IMAGE_OCCLUSION), basic(1))))

    result = await generate(llm, job_request)

    assert fronts(result) == ["front 1"]
    [prompt] = llm.prompts
    assert NoteType.IMAGE_OCCLUSION not in prompt.system + prompt.user


async def test_request_for_only_image_occlusion_yields_no_notes_without_calling_the_model() -> None:
    llm = FakeLlmClient(model_reply(document(valid_note(NoteType.IMAGE_OCCLUSION))))

    result = await generate(llm, request(NoteType.IMAGE_OCCLUSION))

    assert result.notes == ()
    assert llm.prompts == []


def test_every_note_type_except_image_occlusion_has_a_handler_registered_under_its_own_type() -> None:
    assert set(NOTE_TYPE_HANDLERS) == set(NoteType) - {NoteType.IMAGE_OCCLUSION}
    assert all(handler.note_type is note_type for note_type, handler in NOTE_TYPE_HANDLERS.items())


REPAIRABLE_CLOZE: dict[str, tuple[str, list[int]]] = {
    "gap between c1 and c3": ("A {{c1::red}} border means {{c3::prohibition}}", [1, 2]),
    "numbering that starts at c2": ("{{c2::Red}} borders and {{c3::blue}} discs", [1, 2]),
    "numbering that starts at c0": ("{{c0::Red}} borders and {{c1::blue}} discs", [1, 2]),
    "leading zeros": ("{{c01::Red}} borders and {{c002::blue}} discs", [1, 2]),
    "uppercase C": ("{{C1::Red}} borders and {{C2::blue}} discs", [1, 2]),
    "shared number kept together across a gap": ("{{c1::Red}}, {{c1::white}} and {{c4::blue}}", [1, 1, 2]),
    "descending numbers": ("{{c5::Red}} borders and {{c2::blue}} discs", [2, 1]),
    "number too long to convert": ("{{c" + "9" * 5000 + "::Red}} and {{c1::blue}}", [2, 1]),
    "twelve markers renumbered into two digits": (
        " ".join(f"{{{{c{number}::part {number}}}}}" for number in range(1, 24, 2)),
        list(range(1, 13)),
    ),
}


@pytest.mark.parametrize(("text", "numbering"), REPAIRABLE_CLOZE.values(), ids=REPAIRABLE_CLOZE.keys())
async def test_cloze_with_gapped_or_irregular_numbering_is_renumbered_from_one(
    text: str, numbering: list[int]
) -> None:
    llm = FakeLlmClient(model_reply(document(note_json(NoteType.CLOZE, {"text": text}))))

    result = await generate(llm, request(NoteType.CLOZE))

    [note] = result.notes
    assert isinstance(note.fields, ClozeFields)
    assert [int(number) for number in re.findall(r"\{\{c([0-9]+)::", note.fields.text)] == numbering
    assert cloze_numbers(note.fields.text) == set(range(1, max(numbering) + 1))


UNREPAIRABLE_CLOZE = {
    "no marker": "Paris is the capital of France",
    "single colon marker": "{{c1:Paris}} is the capital of France",
    "unterminated marker": "{{c1::Paris is the capital of France",
    "nested markers": "{{c1::Paris is in {{c2::France}}}}",
    "blank answer": "{{c1::   }} is the capital of France",
    "marker spanning lines": "{{c1::Par\nis}} is the capital of France",
    "blank text": "   ",
}


@pytest.mark.parametrize("text", UNREPAIRABLE_CLOZE.values(), ids=UNREPAIRABLE_CLOZE.keys())
async def test_cloze_that_cannot_be_trivially_repaired_is_dropped(text: str) -> None:
    llm = FakeLlmClient(model_reply(document(note_json(NoteType.CLOZE, {"text": text}), basic(1))))

    result = await generate(llm, request(NoteType.CLOZE, NoteType.BASIC))

    assert [note.note_type for note in result.notes] == [NoteType.BASIC]


INVALID_DISTRACTORS: dict[str, list[str]] = {
    "a distractor equal to the answer": ["Octagon", "Circle"],
    "a distractor equal to the answer up to case and spacing": ["  octagon ", "Circle"],
    "a distractor equal to the answer up to an invisible character": [
        "Octa\N{ZERO WIDTH SPACE}gon",
        "Circle",
    ],
    "one distractor": ["Circle"],
    "no distractors": [],
    "five distractors": ["Circle", "Square", "Diamond", "Triangle", "Hexagon"],
    "duplicate distractors": ["Circle", "circle"],
    "a blank distractor": ["Circle", "   "],
}


@pytest.mark.parametrize("distractors", INVALID_DISTRACTORS.values(), ids=INVALID_DISTRACTORS.keys())
async def test_multiple_choice_with_invalid_distractors_is_dropped(distractors: list[str]) -> None:
    fields = {"question": "Which shape is a stop sign?", "answer": "Octagon", "distractors": distractors}
    llm = FakeLlmClient(model_reply(document(note_json(NoteType.MULTIPLE_CHOICE, fields), basic(1))))

    result = await generate(llm, request(NoteType.MULTIPLE_CHOICE, NoteType.BASIC))

    assert [note.note_type for note in result.notes] == [NoteType.BASIC]


@pytest.mark.parametrize("count", [2, 4])
async def test_multiple_choice_with_two_to_four_distinct_wrong_distractors_is_kept(count: int) -> None:
    distractors = ["Circle", "Square", "Diamond", "Triangle"][:count]
    fields = {"question": "Which shape is a stop sign?", "answer": "Octagon", "distractors": distractors}
    llm = FakeLlmClient(model_reply(document(note_json(NoteType.MULTIPLE_CHOICE, fields))))

    result = await generate(llm, request(NoteType.MULTIPLE_CHOICE))

    [note] = result.notes
    assert isinstance(note.fields, MultipleChoiceFields)
    assert len(note.fields.distractors) == count


UNSOURCED: dict[str, object] = {
    "empty list": [],
    "null": None,
    "a bare number": 1,
    "a number beyond the material": [3],
    "zero": [0],
    "a negative number": [-1],
    "a boolean": [True],
    "a numeric string": ["1"],
    "a float": [1.0],
    "a source the model wrote itself": [{"title": "Rules", "url": "https://example.com/made-up"}],
}


@pytest.mark.parametrize("sources", UNSOURCED.values(), ids=UNSOURCED.keys())
async def test_note_without_a_source_from_the_material_is_dropped_and_the_others_kept(
    sources: object,
) -> None:
    unsourced = note_json(NoteType.BASIC, {"front": "front 0", "back": "back 0"}, sources=sources)
    llm = FakeLlmClient(model_reply(document(unsourced, basic(1))))

    result = await generate(llm, request(NoteType.BASIC))

    assert fronts(result) == ["front 1"]


async def test_note_missing_its_sources_key_is_dropped() -> None:
    unsourced = {"noteType": NoteType.BASIC, "fields": VALID_FIELDS[NoteType.BASIC]}
    llm = FakeLlmClient(model_reply(document(unsourced, basic(1))))

    result = await generate(llm, request(NoteType.BASIC))

    assert fronts(result) == ["front 1"]


async def test_citations_resolve_to_the_material_sources_in_order_without_duplicates() -> None:
    cited = note_json(NoteType.BASIC, {"front": "front 1", "back": "back 1"}, sources=[2, 9, "1", 2, 1])
    llm = FakeLlmClient(model_reply(document(cited)))

    result = await generate(llm, request(NoteType.BASIC))

    [note] = result.notes
    assert note.sources == (MATERIAL[1].source, MATERIAL[0].source)


async def test_no_source_material_yields_no_notes_without_calling_the_model() -> None:
    llm = FakeLlmClient(model_reply(document(basic(1))))

    result = await generate(llm, request(NoteType.BASIC), material=())

    assert result.notes == ()
    assert result.deck.title == TOPIC
    assert llm.prompts == []


DIRTY = "\x00\ud800"


def dirty(value: str) -> str:
    return f"{DIRTY}{value[:1]}{DIRTY}{value[1:]}{DIRTY}"


async def test_nul_and_lone_surrogates_are_stripped_from_every_generated_string() -> None:
    job_request = request(*HANDLED_TYPES)
    notes = [
        {
            **note_json(
                note_type,
                {
                    key: dirty(value) if isinstance(value, str) else value
                    for key, value in VALID_FIELDS[note_type].items()
                },
            ),
            "tags": [dirty("signs")],
        }
        for note_type in HANDLED_TYPES
    ]
    notes[-1]["fields"] = {
        **VALID_FIELDS[NoteType.MULTIPLE_CHOICE],
        "distractors": [dirty("Circle"), dirty("Triangle")],
    }
    deck = {"title": dirty("Signs"), "description": dirty("Warning signs"), "tags": [dirty("driving")]}
    llm = FakeLlmClient(model_reply(document(*notes, deck=deck)))

    result = await generate(llm, job_request)

    assert len(result.notes) == len(HANDLED_TYPES)
    body = GenerationResultBody.from_result(result).model_dump(mode="json", by_alias=True)
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    assert b"\\u0000" not in encoded
    assert b"\x00" not in encoded
    assert_valid_for(result, job_request)


async def test_raw_nul_inside_a_json_string_is_stripped() -> None:
    text = json.dumps(document(basic(1))).replace("front 1", "front\x001")

    result = await generate(
        FakeLlmClient(LlmReply(text=text, stop=LlmStop.COMPLETE)), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front1"]


async def test_astral_characters_written_as_surrogate_pairs_survive_the_stripping() -> None:
    fields = {"front": "\N{OCTAGONAL SIGN} means?", "back": "Stop"}
    llm = FakeLlmClient(model_reply(document(note_json(NoteType.BASIC, fields))))

    result = await generate(llm, request(NoteType.BASIC))

    assert fronts(result) == ["\N{OCTAGONAL SIGN} means?"]


async def test_field_made_only_of_unstorable_characters_is_blank_and_dropped() -> None:
    blank = note_json(NoteType.BASIC, {"front": DIRTY, "back": "back 0"})
    llm = FakeLlmClient(model_reply(document(blank, basic(1))))

    result = await generate(llm, request(NoteType.BASIC))

    assert fronts(result) == ["front 1"]


async def test_material_source_title_with_a_nul_is_cleaned_before_it_is_cited() -> None:
    material = (
        SourceMaterial(
            source=Source(title="Traffic\x00 regulations", url="https://example.com/rules"), text="facts"
        ),
    )
    llm = FakeLlmClient(model_reply(document(basic(1))))

    result = await generate(llm, request(NoteType.BASIC), material=material)

    [note] = result.notes
    assert [source.title for source in note.sources] == ["Traffic regulations"]


async def test_lone_surrogates_and_nul_in_the_source_material_never_reach_the_provider() -> None:
    material = (
        SourceMaterial(
            source=Source(title="Traffic\ud800 regulations", url="https://example.com/rules"),
            text="A red triangle\udfff warns\x00 of a hazard.",
        ),
    )
    llm = FakeLlmClient(model_reply(document(basic(1))))

    result = await generate(llm, request(NoteType.BASIC), material=material)

    [prompt] = llm.prompts
    sent = prompt.system + prompt.user
    assert "\x00" not in sent
    assert all(unicodedata.category(character) != "Cs" for character in sent)
    [note] = result.notes
    assert [source.title for source in note.sources] == ["Traffic regulations"]


@pytest.mark.parametrize("card_count", [0, -1])
async def test_non_positive_card_count_yields_no_notes_without_calling_the_model(card_count: int) -> None:
    llm = FakeLlmClient(model_reply(document(basic(1), basic(2), basic(3))))

    result = await generate(llm, request(NoteType.BASIC, card_count=card_count))

    assert result.notes == ()
    assert llm.prompts == []


async def test_more_valid_notes_than_card_count_are_trimmed_to_the_first_card_count() -> None:
    job_request = request(NoteType.BASIC, card_count=5)
    llm = FakeLlmClient(model_reply(document(*(basic(number) for number in range(1, 9)))))

    result = await generate(llm, job_request)

    assert fronts(result) == [f"front {number}" for number in range(1, 6)]
    assert_valid_for(result, job_request)


@pytest.mark.parametrize("returned", [0, 1, 4, 5, 6, 40])
async def test_result_never_exceeds_card_count_and_is_never_padded(returned: int) -> None:
    job_request = request(NoteType.BASIC, card_count=5)
    llm = FakeLlmClient(model_reply(document(*(basic(number) for number in range(1, returned + 1)))))

    result = await generate(llm, job_request)

    assert len(result.notes) == min(returned, job_request.card_count)
    assert len(llm.prompts) == 1


async def test_dropped_notes_do_not_count_towards_card_count() -> None:
    invalid = [note_json(NoteType.BASIC, {"front": "f"}) for _ in range(3)]
    valid = [basic(number) for number in range(1, 6)]
    llm = FakeLlmClient(model_reply(document(*invalid, *valid)))

    result = await generate(llm, request(NoteType.BASIC, card_count=5))

    assert fronts(result) == [f"front {number}" for number in range(1, 6)]


async def test_document_repeated_verbatim_in_a_code_fence_yields_each_note_once() -> None:
    text = json.dumps(document(basic(1), basic(2)))
    reply = f"{text}\n\nFormatted:\n```json\n{text}\n```"

    result = await generate(
        FakeLlmClient(LlmReply(text=reply, stop=LlmStop.COMPLETE)), request(NoteType.BASIC)
    )

    assert fronts(result) == ["front 1", "front 2"]


async def test_duplicates_do_not_count_towards_card_count() -> None:
    llm = FakeLlmClient(model_reply(document(basic(1), basic(1), basic(2))))

    result = await generate(llm, request(NoteType.BASIC, card_count=2))

    assert fronts(result) == ["front 1", "front 2"]


async def test_first_of_two_duplicates_is_kept_with_its_own_sources() -> None:
    fields = {"front": "front 1", "back": "back 1"}
    llm = FakeLlmClient(
        model_reply(
            document(
                note_json(NoteType.BASIC, fields, sources=[1]), note_json(NoteType.BASIC, fields, sources=[2])
            )
        )
    )

    result = await generate(llm, request(NoteType.BASIC))

    [note] = result.notes
    assert note.sources == (MATERIAL[0].source,)


@pytest.mark.parametrize(
    "repeated",
    [basic(1), note_json(NoteType.BASIC, {"front": "FRONT 1", "back": "BACK 1"})],
    ids=["exact", "near"],
)
async def test_duplicates_are_only_looked_for_within_one_generation(repeated: dict[str, object]) -> None:
    ids = sequential_job_ids()
    generator = LlmCardGenerator(
        llm=FakeLlmClient(model_reply(document(basic(1))), model_reply(document(repeated))),
        new_id=lambda: next(ids),
        handlers=NOTE_TYPE_HANDLERS,
    )

    first = await generator.generate(job_id(1), request(NoteType.BASIC), MATERIAL)
    second = await generator.generate(job_id(2), request(NoteType.BASIC), MATERIAL)

    assert (len(first.notes), len(second.notes)) == (1, 1)


async def test_notes_that_match_once_validation_trims_them_are_duplicates() -> None:
    padded = note_json(NoteType.BASIC, {"front": "  front 1\n", "back": "back 1 "})
    llm = FakeLlmClient(model_reply(document(basic(1), padded)))

    result = await generate(llm, request(NoteType.BASIC))

    assert fronts(result) == ["front 1"]


NEAR_DUPLICATES: dict[str, tuple[dict[str, object], dict[str, object]]] = {
    "text that differs only in case": (
        basic(1),
        note_json(NoteType.BASIC, {"front": "FRONT 1", "back": "Back 1"}),
    ),
    "text that differs only in unicode normalization": (
        note_json(NoteType.BASIC, {"front": "Caf\N{LATIN SMALL LETTER E WITH ACUTE}", "back": "back"}),
        note_json(NoteType.BASIC, {"front": "Cafe\N{COMBINING ACUTE ACCENT}", "back": "back"}),
    ),
    "text that differs only in width": (
        basic(1),
        note_json(NoteType.BASIC, {"front": "\N{FULLWIDTH LATIN SMALL LETTER F}ront 1", "back": "back 1"}),
    ),
    "text that differs only in inner whitespace": (
        basic(1),
        note_json(NoteType.BASIC, {"front": "front\t 1", "back": "back\n1"}),
    ),
    "text that differs only in invisible characters": (
        basic(1),
        note_json(
            NoteType.BASIC,
            {"front": "fr\N{ZERO WIDTH SPACE}ont 1", "back": "back 1\N{RIGHT-TO-LEFT MARK}"},
        ),
    ),
    "reversed sides that differ only in case": (
        valid_note(NoteType.BASIC_REVERSED),
        with_fields(NoteType.BASIC_REVERSED, front="STOP SIGN"),
    ),
    "type-in answer that differs only in case": (
        valid_note(NoteType.BASIC_TYPE_IN),
        with_fields(NoteType.BASIC_TYPE_IN, back="octagon"),
    ),
    "reverse card asked for by only one of them": (
        valid_note(NoteType.BASIC_OPTIONAL_REVERSED),
        with_fields(NoteType.BASIC_OPTIONAL_REVERSED, addReverse=False),
    ),
    "cloze text that differs only in case": (
        valid_note(NoteType.CLOZE),
        with_fields(NoteType.CLOZE, text="a {{c1::Red Triangle}} warns of a {{c2::Hazard}}"),
    ),
    "cloze with other extra context": (
        valid_note(NoteType.CLOZE),
        with_fields(NoteType.CLOZE, extra="Traffic code, section 5"),
    ),
    "multiple choice with distractors in another order": (
        valid_note(NoteType.MULTIPLE_CHOICE),
        with_fields(NoteType.MULTIPLE_CHOICE, distractors=["Triangle", "Circle"]),
    ),
    "multiple choice that differs only in case": (
        valid_note(NoteType.MULTIPLE_CHOICE),
        with_fields(
            NoteType.MULTIPLE_CHOICE,
            question="which shape is a STOP sign?",
            answer="octagon",
            distractors=["triangle", "CIRCLE"],
        ),
    ),
}


@pytest.mark.parametrize(("first", "second"), NEAR_DUPLICATES.values(), ids=NEAR_DUPLICATES.keys())
async def test_near_duplicate_is_dropped_and_the_first_note_is_kept(
    first: dict[str, object], second: dict[str, object]
) -> None:
    job_request = request(*HANDLED_TYPES)
    alone = await generate(FakeLlmClient(model_reply(document(first))), job_request)

    result = await generate(FakeLlmClient(model_reply(document(first, second))), job_request)

    [kept] = result.notes
    [expected] = alone.notes
    assert kept.fields == expected.fields


async def test_near_duplicate_is_logged_as_a_duplicate_and_does_not_use_up_card_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    near = note_json(NoteType.BASIC, {"front": "FRONT 1", "back": "BACK 1"})
    llm = FakeLlmClient(model_reply(document(basic(1), near, basic(2))))

    with caplog.at_level(logging.INFO, logger=GENERATOR_LOGGER):
        result = await generate(llm, request(NoteType.BASIC, card_count=2))

    assert fronts(result) == ["front 1", "front 2"]
    [record] = [record for record in caplog.records if record.getMessage() == "card_generation_finished"]
    assert record.__dict__["dropped_notes"] == {"duplicate": 1}


async def test_note_dropped_as_invalid_does_not_make_its_near_duplicate_a_duplicate() -> None:
    unsourced = note_json(NoteType.BASIC, {"front": "front 1", "back": "back 1"}, sources=[])
    near = note_json(NoteType.BASIC, {"front": "FRONT 1", "back": "BACK 1"})
    llm = FakeLlmClient(model_reply(document(unsourced, near)))

    result = await generate(llm, request(NoteType.BASIC))

    assert fronts(result) == ["FRONT 1"]


DISTINCT_NOTES: dict[str, tuple[dict[str, object], dict[str, object]]] = {
    "same fields under another note type": (
        note_json(NoteType.BASIC, VALID_FIELDS[NoteType.BASIC_REVERSED]),
        valid_note(NoteType.BASIC_REVERSED),
    ),
    "same front with another back": (
        basic(1),
        note_json(NoteType.BASIC, {"front": "front 1", "back": "back 2"}),
    ),
    "reversed sides swapped": (
        valid_note(NoteType.BASIC_REVERSED),
        with_fields(NoteType.BASIC_REVERSED, front="Red octagon", back="Stop sign"),
    ),
    "superscript and subscript digits": (
        note_json(NoteType.BASIC, {"front": "x\N{SUPERSCRIPT TWO}", "back": "back"}),
        note_json(NoteType.BASIC, {"front": "x\N{SUBSCRIPT TWO}", "back": "back"}),
    ),
    "sharp s and double s": (
        note_json(NoteType.BASIC, {"front": "Stra\N{LATIN SMALL LETTER SHARP S}e", "back": "back"}),
        note_json(NoteType.BASIC, {"front": "Strasse", "back": "back"}),
    ),
    "cloze hiding another part": (
        valid_note(NoteType.CLOZE),
        with_fields(NoteType.CLOZE, text="A red triangle warns of a {{c1::hazard}}"),
    ),
    "multiple choice with another distractor": (
        valid_note(NoteType.MULTIPLE_CHOICE),
        with_fields(NoteType.MULTIPLE_CHOICE, distractors=["Circle", "Square"]),
    ),
    "multiple choice with one distractor more": (
        with_fields(NoteType.MULTIPLE_CHOICE, distractors=["Circle", "Triangle", "Square"]),
        valid_note(NoteType.MULTIPLE_CHOICE),
    ),
    "multiple choice with another answer": (
        valid_note(NoteType.MULTIPLE_CHOICE),
        with_fields(NoteType.MULTIPLE_CHOICE, answer="Hexagon"),
    ),
}


@pytest.mark.parametrize(("first", "second"), DISTINCT_NOTES.values(), ids=DISTINCT_NOTES.keys())
async def test_notes_that_ask_different_things_are_both_kept(
    first: dict[str, object], second: dict[str, object]
) -> None:
    llm = FakeLlmClient(model_reply(document(first, second)))

    result = await generate(llm, request(*HANDLED_TYPES))

    assert len(result.notes) == 2


async def test_refused_reply_is_dropped_whole_even_when_it_carries_valid_notes() -> None:
    llm = FakeLlmClient(model_reply(document(basic(1), basic(2)), LlmStop.REFUSED))

    result = await generate(llm, request(NoteType.BASIC))

    assert result.notes == ()
    assert result.deck.title == TOPIC
    assert len(llm.prompts) == 1


async def test_deck_title_description_and_tags_come_from_the_model_when_valid() -> None:
    deck = {"title": "  Signs of the road ", "description": "Warning signs", "tags": ["a", " a ", "", 5, "b"]}

    result = await generate(
        FakeLlmClient(model_reply(document(basic(1), deck=deck))), request(NoteType.BASIC)
    )

    assert (result.deck.title, result.deck.description, result.deck.tags) == (
        "Signs of the road",
        "Warning signs",
        ("a", "b"),
    )


UNUSABLE_DECKS: dict[str, object] = {
    "missing": None,
    "not an object": "Signs",
    "blank title": {"title": " \N{ZERO WIDTH SPACE} "},
    "numeric title": {"title": 5},
    "title made of unstorable characters": {"title": DIRTY},
}


@pytest.mark.parametrize("deck", UNUSABLE_DECKS.values(), ids=UNUSABLE_DECKS.keys())
async def test_deck_without_a_usable_title_falls_back_to_the_topic(deck: object) -> None:
    result = await generate(
        FakeLlmClient(model_reply(document(basic(1), deck=deck))), request(NoteType.BASIC)
    )

    assert result.deck.title == TOPIC


async def test_over_long_deck_title_and_description_are_cut_to_their_utf16_limits() -> None:
    deck = {"title": "\N{OCTAGONAL SIGN}" * 61, "description": "a" * 400 + "\N{OCTAGONAL SIGN}" * 60}

    result = await generate(
        FakeLlmClient(model_reply(document(basic(1), deck=deck))), request(NoteType.BASIC)
    )

    assert result.deck.title == "\N{OCTAGONAL SIGN}" * 60
    assert result.deck.description is not None
    assert utf16_length(result.deck.description) == MAX_DESCRIPTION_LENGTH


async def test_blank_deck_description_becomes_absent() -> None:
    deck = {"title": "Signs", "description": "  "}

    result = await generate(
        FakeLlmClient(model_reply(document(basic(1), deck=deck))), request(NoteType.BASIC)
    )

    assert result.deck.description is None


async def test_fallback_title_skips_leading_invisible_characters_of_a_long_topic() -> None:
    job_request = GenerationRequest(
        topic="\N{ZERO WIDTH SPACE}" * 150 + "Road signs",
        language="en",
        card_count=5,
        difficulty=Difficulty.BEGINNER,
        note_types=(NoteType.BASIC,),
        include_images=False,
        instructions=None,
    )

    result = await generate(FakeLlmClient(model_reply([basic(1)])), job_request)

    assert not is_blank(result.deck.title)
    assert utf16_length(result.deck.title) <= MAX_TITLE_LENGTH


async def test_prompt_offers_only_the_requested_types_and_numbers_the_material_without_urls() -> None:
    llm = FakeLlmClient(model_reply(document(basic(1))))

    await generate(llm, request(NoteType.BASIC, NoteType.CLOZE))

    [prompt] = llm.prompts
    offered = {note_type for note_type in NoteType if f'"{note_type}"' in prompt.system}
    assert offered == {NoteType.BASIC, NoteType.CLOZE}
    assert all(f'<source number="{number}">' in prompt.user for number in (1, 2))
    assert all(entry.text in prompt.user for entry in MATERIAL)
    assert all(entry.source.url not in prompt.system + prompt.user for entry in MATERIAL)
    assert "json" in prompt.system.lower()


@pytest.mark.parametrize("requested", HANDLED_TYPES)
async def test_prompt_offers_exactly_the_requested_type(requested: NoteType) -> None:
    llm = FakeLlmClient(model_reply(document(basic(1))))

    await generate(llm, request(requested))

    [prompt] = llm.prompts
    assert {note_type for note_type in NoteType if f'"{note_type}"' in prompt.system} == {requested}


@pytest.mark.parametrize("difficulty", Difficulty)
async def test_every_difficulty_can_be_prompted(difficulty: Difficulty) -> None:
    llm = FakeLlmClient(model_reply(document(basic(1))))
    job_request = GenerationRequest(
        topic=TOPIC,
        language="en",
        card_count=5,
        difficulty=difficulty,
        note_types=(NoteType.BASIC,),
        include_images=False,
        instructions=None,
    )

    result = await generate(llm, job_request)

    assert len(result.notes) == 1
    assert difficulty in llm.prompts[0].user


async def test_prompt_carries_user_instructions_only_when_there_are_some() -> None:
    llm = FakeLlmClient(model_reply(document(basic(1))))
    steered = GenerationRequest(
        topic=TOPIC,
        language="ru",
        card_count=5,
        difficulty=Difficulty.ADVANCED,
        note_types=(NoteType.BASIC,),
        include_images=False,
        instructions="Focus on warning signs",
    )

    await generate(llm, steered)
    await generate(llm, request(NoteType.BASIC))

    with_instructions, without_instructions = llm.prompts
    assert "Focus on warning signs" in with_instructions.user
    assert "User instructions" not in without_instructions.user


async def test_outcome_is_logged_with_drop_counts_by_reason_and_no_note_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notes = [
        basic(1),
        basic(1),
        valid_note(NoteType.CLOZE),
        note_json(NoteType.BASIC, {"front": "secret"}),
        basic(2),
    ]
    llm = FakeLlmClient(model_reply(document(*notes)))

    with caplog.at_level(logging.INFO, logger=GENERATOR_LOGGER):
        await generate(llm, request(NoteType.BASIC, card_count=1))

    [record] = [record for record in caplog.records if record.getMessage() == "card_generation_finished"]
    assert record.__dict__["dropped_notes"] == {
        "duplicate": 1,
        "unrequested_type": 1,
        "mismatched_fields": 1,
        "over_card_count": 1,
    }
    assert "secret" not in json.dumps(record.__dict__, default=str)


LOGGED_OUTCOMES: dict[str, tuple[tuple[SourceMaterial, ...], str]] = {
    "finished": (MATERIAL, "card_generation_finished"),
    "skipped for lack of material": ((), "card_generation_skipped"),
}


@pytest.mark.parametrize(("material", "message"), LOGGED_OUTCOMES.values(), ids=LOGGED_OUTCOMES.keys())
async def test_outcome_log_line_names_the_job_it_came_from(
    material: tuple[SourceMaterial, ...], message: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=GENERATOR_LOGGER):
        await generate(FakeLlmClient(model_reply(document(basic(1)))), request(NoteType.BASIC), material)

    [record] = [record for record in caplog.records if record.getMessage() == message]
    assert record.__dict__["job_id"] == str(JOB_ID)


QUICK_POLICY = RetryPolicy(
    max_attempts=2,
    attempt_timeout_seconds=0.05,
    deadline_seconds=1,
    base_delay_seconds=0,
    max_delay_seconds=0,
)


MODEL_MAX_OUTPUT_TOKENS = 16_000


def resilient(
    llm: LlmClient, breaker: CircuitBreaker | None = None, policy: RetryPolicy = QUICK_POLICY
) -> ResilientLlmClient:
    circuit = breaker or CircuitBreaker(failure_threshold=5, reset_seconds=30, clock=lambda: 0.0)
    runtime = RetryRuntime(clock=lambda: 0.0, sleep=asyncio.sleep, jitter=lambda: 0.0)
    return ResilientLlmClient(llm, ResilientCaller(policy, circuit, runtime), MODEL_MAX_OUTPUT_TOKENS)


DECK_SIZES: dict[str, tuple[int, CircuitState]] = {
    "the smallest deck": (5, CircuitState.OPEN),
    "a 50-card deck": (50, CircuitState.OPEN),
    "the largest deck": (200, CircuitState.CLOSED),
}


@pytest.mark.parametrize(("card_count", "state"), DECK_SIZES.values(), ids=DECK_SIZES.keys())
async def test_model_timeouts_count_against_the_provider_unless_the_deck_is_oversized(
    card_count: int, state: CircuitState
) -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30, clock=lambda: 0.0)
    llm = resilient(FakeLlmClient(hang_forever), breaker)

    with pytest.raises(UpstreamUnavailableError):
        await generate(llm, request(NoteType.BASIC, card_count=card_count))

    assert breaker.state is state


def pipeline_for(harness: Harness, llm: LlmClient) -> RunGeneration:
    ids = sequential_job_ids()
    return RunGeneration(
        store=harness.store,
        retriever=harness.providers,
        parser=harness.providers,
        generator=LlmCardGenerator(llm=llm, new_id=lambda: next(ids), handlers=NOTE_TYPE_HANDLERS),
        media=harness.providers,
        clock=lambda: harness.now,
    )


async def run_job(llm: LlmClient) -> GenerationJob:
    harness = Harness()
    job_id = (await harness.create(generation_request(), scope())).job.job_id
    await pipeline_for(harness, llm)(job_id)
    return await harness.get(job_id)


PATIENT_POLICY = RetryPolicy(
    max_attempts=2,
    attempt_timeout_seconds=MODEL_THINKING_SECONDS * 10,
    deadline_seconds=MODEL_THINKING_SECONDS * 30,
    base_delay_seconds=0,
    max_delay_seconds=0,
)


async def test_cancel_during_the_model_call_aborts_the_call_and_leaves_the_job_cancelled() -> None:
    harness = Harness()
    model = SlowModel(model_reply(document(basic(1))))
    llm = FakeLlmClient(model.think)
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30, clock=lambda: 0.0)
    job_id = (await harness.create(generation_request(), scope())).job.job_id
    running = asyncio.create_task(pipeline_for(harness, resilient(llm, breaker, PATIENT_POLICY))(job_id))
    harness.queue.running[job_id] = running
    await model.reached.wait()

    await harness.cancel(job_id)

    with pytest.raises(asyncio.CancelledError):
        await running
    job = await harness.get(job_id)
    assert isinstance(job.state, Cancelled)
    assert job.stage is JobStage.GENERATING_CARDS
    assert model.endings == ["aborted"]
    assert len(llm.prompts) == 1
    assert breaker.state is CircuitState.CLOSED


PIPELINE_FAILURES: dict[str, tuple[Callable[[], LlmOutcome], FailureCode]] = {
    "provider outage": (lambda: LlmUnavailableError("down"), FailureCode.PROVIDER_UNAVAILABLE),
    "provider timeout": (lambda: hang_forever, FailureCode.PROVIDER_UNAVAILABLE),
    "request rejected": (lambda: LlmRejectedError("401"), FailureCode.GENERATION_FAILED),
    "malformed provider response": (lambda: LlmResponseError("no choices"), FailureCode.GENERATION_FAILED),
    "unusable reply": (lambda: LlmReply(text="no json", stop=LlmStop.COMPLETE), FailureCode.NO_VALID_CONTENT),
    "refused reply": (
        lambda: model_reply(document(basic(1)), LlmStop.REFUSED),
        FailureCode.NO_VALID_CONTENT,
    ),
}


@pytest.mark.parametrize(("outcome", "code"), PIPELINE_FAILURES.values(), ids=PIPELINE_FAILURES.keys())
async def test_pipeline_maps_each_adapter_outcome_to_its_failure_code(
    outcome: Callable[[], LlmOutcome], code: FailureCode
) -> None:
    job = await run_job(resilient(FakeLlmClient(outcome())))

    assert isinstance(job.state, Failed)
    assert (job.state.code, job.stage) == (code, JobStage.GENERATING_CARDS)


async def test_pipeline_fails_fast_with_provider_unavailable_while_the_circuit_is_open() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=30, clock=lambda: 0.0)
    breaker.record_failure()
    llm = FakeLlmClient(model_reply(document(basic(1))))

    job = await run_job(resilient(llm, breaker))

    assert isinstance(job.state, Failed)
    assert job.state.code is FailureCode.PROVIDER_UNAVAILABLE
    assert breaker.state is CircuitState.OPEN
    assert llm.prompts == []


async def test_pipeline_succeeds_with_the_valid_notes_of_a_generated_reply() -> None:
    llm = FakeLlmClient(model_reply(document(basic(1), note_json(NoteType.CLOZE, {"text": "x"}), basic(2))))

    job = await run_job(resilient(llm))

    assert isinstance(job.state, Succeeded)
    assert len(job.state.result.notes) == 2
