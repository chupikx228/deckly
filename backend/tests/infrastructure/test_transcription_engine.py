import io
import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from deckly.infrastructure.transcription.client import SAMPLE_RATE
from deckly.infrastructure.transcription.engine import (
    BEAM_SIZE,
    BOUNDARY_LOOKBACK_SAMPLES,
    CHUNK_SAMPLES,
    ENERGY_FRAME_SAMPLES,
    SpeechModel,
    chunk_bounds,
    chunk_segments,
    main,
    parse_arguments,
    quietest_cut,
    read_samples,
    transcribe_samples,
)
from deckly.infrastructure.transcription.engine_output import EngineOutput, EngineSegment

LOUD = 20_000
QUIET = 10


@dataclass(frozen=True, slots=True)
class Spoken:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Detected:
    language: str


@dataclass
class ScriptedModel:
    replies: list[tuple[list[Spoken], str]]
    calls: list[tuple[int, str | None, int, bool]] = field(default_factory=list)

    def transcribe(
        self, audio: NDArray[np.float32], *, language: str | None, beam_size: int, vad_filter: bool
    ) -> tuple[Iterable[Spoken], Detected]:
        self.calls.append((len(audio), language, beam_size, vad_filter))
        segments, detected = self.replies.pop(0)
        return iter(segments), Detected(detected)


def loud(samples: int) -> NDArray[np.int16]:
    return np.full(samples, LOUD, dtype=np.int16)


def test_cut_falls_in_the_quietest_frame_of_the_lookback_window() -> None:
    samples = loud(CHUNK_SAMPLES + SAMPLE_RATE)
    quiet_start = CHUNK_SAMPLES - 3 * SAMPLE_RATE
    samples[quiet_start : quiet_start + ENERGY_FRAME_SAMPLES] = QUIET

    cut = quietest_cut(samples, 0, CHUNK_SAMPLES)

    assert cut == quiet_start + ENERGY_FRAME_SAMPLES // 2


def test_cut_never_reaches_back_before_the_lookback_window_or_the_chunk_start() -> None:
    samples = loud(CHUNK_SAMPLES * 2)
    samples[: ENERGY_FRAME_SAMPLES * 4] = QUIET

    cut = quietest_cut(samples, 0, CHUNK_SAMPLES)

    assert CHUNK_SAMPLES - BOUNDARY_LOOKBACK_SAMPLES <= cut <= CHUNK_SAMPLES


def test_window_shorter_than_one_energy_frame_cuts_where_it_was_asked() -> None:
    samples = loud(ENERGY_FRAME_SAMPLES)

    assert quietest_cut(samples, 10, 20) == 20


def test_audio_shorter_than_a_chunk_is_one_chunk() -> None:
    assert chunk_bounds(loud(SAMPLE_RATE * 3)) == ((0, SAMPLE_RATE * 3),)


def test_chunks_cover_every_sample_once_in_order_and_each_stays_within_the_chunk_length() -> None:
    samples = loud(CHUNK_SAMPLES * 3 + 12_345)

    bounds = chunk_bounds(samples)

    assert bounds[0][0] == 0
    assert bounds[-1][1] == len(samples)
    assert all(previous[1] == following[0] for previous, following in itertools.pairwise(bounds))
    assert all(0 < end - start <= CHUNK_SAMPLES for start, end in bounds)


def test_empty_audio_has_no_chunks() -> None:
    assert chunk_bounds(np.zeros(0, dtype=np.int16)) == ()


def test_segments_are_shifted_by_their_chunk_offset_and_blank_ones_are_dropped() -> None:
    found = [Spoken(0.5, 2.25, "  First sentence. "), Spoken(2.25, 3.0, "   "), Spoken(3.0, 4.5, "Second.")]

    segments = chunk_segments(found, offset_ms=120_000, limit_ms=240_000)

    assert segments == [
        EngineSegment(start_ms=120_500, end_ms=122_250, text="First sentence."),
        EngineSegment(start_ms=123_000, end_ms=124_500, text="Second."),
    ]


def test_segment_that_ends_before_it_starts_or_before_zero_is_clamped_rather_than_dropped() -> None:
    segments = chunk_segments(
        [Spoken(-0.2, 1.0, "early"), Spoken(5.0, 4.0, "backwards")], offset_ms=0, limit_ms=10_000
    )

    assert segments == [
        EngineSegment(start_ms=0, end_ms=1000, text="early"),
        EngineSegment(start_ms=5000, end_ms=5000, text="backwards"),
    ]


def test_segment_the_model_times_past_the_end_of_its_chunk_is_cut_at_the_chunk_end() -> None:
    segments = chunk_segments(
        [Spoken(11.48, 11.94, "On"), Spoken(12.0, 13.0, "beyond")], offset_ms=0, limit_ms=11_500
    )

    assert segments == [EngineSegment(start_ms=11_480, end_ms=11_500, text="On")]


def test_no_segment_ever_ends_after_the_decoded_audio() -> None:
    samples = loud(SAMPLE_RATE * 3)
    model = ScriptedModel(replies=[([Spoken(0.0, 2.0, "Spoken."), Spoken(2.9, 30.0, "Hallucinated.")], "en")])

    output = transcribe_samples(model, samples)

    assert max(segment.end_ms for segment in output.segments) == 3000


def test_language_is_detected_until_a_chunk_has_speech_and_then_kept_for_the_rest() -> None:
    samples = loud(CHUNK_SAMPLES * 2 + SAMPLE_RATE)
    model = ScriptedModel(
        replies=[([], "en"), ([Spoken(1.0, 2.0, "Привет.")], "ru"), ([Spoken(0.0, 1.0, "Дальше.")], "uk")]
    )

    output = transcribe_samples(model, samples)

    assert output.language == "ru"
    assert [language for _, language, _, _ in model.calls] == [None, None, "ru"]
    assert all(beam == BEAM_SIZE and vad for _, _, beam, vad in model.calls)
    assert [segment.text for segment in output.segments] == ["Привет.", "Дальше."]
    assert output.segments[0].start_ms > CHUNK_SAMPLES * 1000 // SAMPLE_RATE - 10_000


def test_audio_without_speech_has_no_language_and_no_segments() -> None:
    output = transcribe_samples(ScriptedModel(replies=[([], "en")]), loud(SAMPLE_RATE))

    assert output == EngineOutput(language=None, segments=())


def test_model_receives_float_audio_scaled_to_full_scale() -> None:
    received: list[NDArray[np.float32]] = []

    class Recording(ScriptedModel):
        def transcribe(
            self, audio: NDArray[np.float32], *, language: str | None, beam_size: int, vad_filter: bool
        ) -> tuple[Iterable[Spoken], Detected]:
            received.append(audio)
            return super().transcribe(audio, language=language, beam_size=beam_size, vad_filter=vad_filter)

    transcribe_samples(Recording(replies=[([], "en")]), np.array([16_384, -32_768], dtype=np.int16))

    [audio] = received
    assert audio.dtype == np.float32
    assert audio.tolist() == [0.5, -1.0]


def test_engine_reads_at_most_the_samples_it_was_told_to(tmp_path: Path) -> None:
    pcm = tmp_path / "audio.pcm"
    pcm.write_bytes(np.arange(10, dtype=np.int16).tobytes())

    assert read_samples(pcm, 4).tolist() == [0, 1, 2, 3]


def test_engine_drops_a_trailing_half_sample(tmp_path: Path) -> None:
    pcm = tmp_path / "audio.pcm"
    pcm.write_bytes(np.arange(3, dtype=np.int16).tobytes() + b"\x01")

    assert read_samples(pcm, 100).tolist() == [0, 1, 2]


@pytest.mark.parametrize("flag", ["--threads", "--samples"])
@pytest.mark.parametrize("value", ["0", "-3", "many"])
def test_engine_refuses_counts_that_are_not_positive(flag: str, value: str) -> None:
    arguments = {"--model": "/models/small", "--threads": "2", "--samples": "16000", flag: value}

    with pytest.raises(SystemExit):
        parse_arguments([*[item for pair in arguments.items() for item in pair], "--", "audio.pcm"])


def test_engine_main_loads_the_named_model_and_writes_one_json_document(tmp_path: Path) -> None:
    pcm = tmp_path / "audio.pcm"
    pcm.write_bytes(loud(SAMPLE_RATE).tobytes())
    loaded: list[tuple[Path, int]] = []
    model = ScriptedModel(replies=[([Spoken(0.0, 1.0, "Hello.")], "en")])

    def load(directory: Path, threads: int) -> SpeechModel:
        loaded.append((directory, threads))
        return model

    out = io.StringIO()
    models = tmp_path / "models"
    models.mkdir()
    main(
        ["--model", str(models), "--threads", "3", "--samples", str(SAMPLE_RATE), "--", str(pcm)],
        load,
        out,
    )

    assert loaded == [(models, 3)]
    assert EngineOutput.model_validate_json(out.getvalue()) == EngineOutput(
        language="en", segments=(EngineSegment(start_ms=0, end_ms=1000, text="Hello."),)
    )


@pytest.mark.parametrize("directory", ["models/faster-whisper-small", "/nonexistent/model"])
def test_engine_refuses_a_model_directory_it_cannot_load_from_rather_than_asking_the_hub(
    tmp_path: Path, directory: str
) -> None:
    pcm = tmp_path / "audio.pcm"
    pcm.write_bytes(loud(SAMPLE_RATE).tobytes())

    def load(model_directory: Path, threads: int) -> SpeechModel:
        message = f"must not load {model_directory} with {threads} threads"
        raise AssertionError(message)

    with pytest.raises(SystemExit, match="not an existing absolute path"):
        main(["--model", directory, "--threads", "1", "--samples", "1", "--", str(pcm)], load, io.StringIO())


def test_segment_the_model_places_after_the_end_of_its_chunk_has_no_audio_and_is_dropped() -> None:
    segments = chunk_segments(
        [Spoken(10.0, 11.5, "Spoken."), Spoken(11.5, 12.0, "Thank you."), Spoken(13.0, 14.0, "Bye.")],
        offset_ms=0,
        limit_ms=11_500,
    )

    assert [segment.text for segment in segments] == ["Spoken."]
