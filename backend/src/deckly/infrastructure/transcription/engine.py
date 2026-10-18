import argparse
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

import numpy as np
from faster_whisper import WhisperModel
from numpy.typing import NDArray

from deckly.infrastructure.transcription.client import (
    MILLISECONDS_PER_SECOND,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
)
from deckly.infrastructure.transcription.engine_output import EngineOutput, EngineSegment

DEVICE = "cpu"
COMPUTE_TYPE = "int8"
BEAM_SIZE = 1
MODEL_WORKERS = 1
CHUNK_SECONDS = 120
BOUNDARY_LOOKBACK_SECONDS = 10
ENERGY_FRAMES_PER_SECOND = 10
FULL_SCALE = 32768.0
CHUNK_SAMPLES = CHUNK_SECONDS * SAMPLE_RATE
BOUNDARY_LOOKBACK_SAMPLES = BOUNDARY_LOOKBACK_SECONDS * SAMPLE_RATE
ENERGY_FRAME_SAMPLES = SAMPLE_RATE // ENERGY_FRAMES_PER_SECOND

type Samples = NDArray[np.int16]


class SpeechSegment(Protocol):
    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def text(self) -> str: ...


class DetectedLanguage(Protocol):
    @property
    def language(self) -> str: ...


class SpeechModel(Protocol):
    def transcribe(
        self, audio: NDArray[np.float32], *, language: str | None, beam_size: int, vad_filter: bool
    ) -> tuple[Iterable[SpeechSegment], DetectedLanguage]: ...


type ModelLoader = Callable[[Path, int], SpeechModel]


@dataclass(frozen=True, slots=True)
class EngineArguments:
    model_directory: Path
    cpu_threads: int
    samples: int
    audio: Path


def load_whisper_model(model_directory: Path, cpu_threads: int) -> SpeechModel:
    return WhisperModel(
        str(model_directory),
        device=DEVICE,
        compute_type=COMPUTE_TYPE,
        cpu_threads=cpu_threads,
        num_workers=MODEL_WORKERS,
        local_files_only=True,
    )


def quietest_cut(samples: Samples, start: int, end: int) -> int:
    window_start = max(start, end - BOUNDARY_LOOKBACK_SAMPLES)
    frames = (end - window_start) // ENERGY_FRAME_SAMPLES
    if frames == 0:
        return end
    window = samples[window_start : window_start + frames * ENERGY_FRAME_SAMPLES].astype(np.float64)
    energy = np.square(window.reshape(frames, ENERGY_FRAME_SAMPLES)).mean(axis=1)
    return window_start + int(np.argmin(energy)) * ENERGY_FRAME_SAMPLES + ENERGY_FRAME_SAMPLES // 2


def chunk_bounds(samples: Samples, chunk_samples: int = CHUNK_SAMPLES) -> tuple[tuple[int, int], ...]:
    bounds: list[tuple[int, int]] = []
    start, total = 0, len(samples)
    while start < total:
        end = start + chunk_samples
        end = total if end >= total else quietest_cut(samples, start, end)
        bounds.append((start, end))
        start = end
    return tuple(bounds)


def milliseconds(samples: int) -> int:
    return samples * MILLISECONDS_PER_SECOND // SAMPLE_RATE


def seconds_to_milliseconds(seconds: float) -> int:
    return max(0, round(seconds * MILLISECONDS_PER_SECOND))


def chunk_segments(found: Iterable[SpeechSegment], offset_ms: int, limit_ms: int) -> list[EngineSegment]:
    segments: list[EngineSegment] = []
    for segment in found:
        text = segment.text.strip()
        start_ms = offset_ms + seconds_to_milliseconds(segment.start)
        if not text or start_ms >= limit_ms:
            continue
        end_ms = min(max(start_ms, offset_ms + seconds_to_milliseconds(segment.end)), limit_ms)
        segments.append(EngineSegment(start_ms=start_ms, end_ms=end_ms, text=text))
    return segments


def transcribe_samples(model: SpeechModel, samples: Samples) -> EngineOutput:
    language: str | None = None
    segments: list[EngineSegment] = []
    for start, end in chunk_bounds(samples):
        audio = samples[start:end].astype(np.float32) / FULL_SCALE
        found, detected = model.transcribe(audio, language=language, beam_size=BEAM_SIZE, vad_filter=True)
        transcribed = chunk_segments(found, milliseconds(start), milliseconds(end))
        if transcribed and language is None:
            language = detected.language
        segments.extend(transcribed)
    return EngineOutput(language=language, segments=tuple(segments))


def read_samples(path: Path, samples: int) -> Samples:
    with path.open("rb") as file:
        raw = file.read(samples * SAMPLE_WIDTH_BYTES)
    return np.frombuffer(raw[: len(raw) - len(raw) % SAMPLE_WIDTH_BYTES], dtype=np.int16)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        message = f"{value} is not a positive integer"
        raise argparse.ArgumentTypeError(message)
    return number


def parse_arguments(argv: Sequence[str]) -> EngineArguments:
    parser = argparse.ArgumentParser(prog="deckly-transcription-engine")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--threads", type=positive, required=True)
    parser.add_argument("--samples", type=positive, required=True)
    parser.add_argument("audio", type=Path)
    namespace = parser.parse_args(argv)
    return EngineArguments(
        model_directory=Path(str(namespace.model)),
        cpu_threads=int(namespace.threads),
        samples=int(namespace.samples),
        audio=Path(str(namespace.audio)),
    )


def main(argv: Sequence[str], load: ModelLoader = load_whisper_model, out: TextIO = sys.stdout) -> None:
    arguments = parse_arguments(argv)
    if not arguments.model_directory.is_absolute() or not arguments.model_directory.is_dir():
        message = (
            f"the transcription model directory {arguments.model_directory} is not an existing absolute path"
        )
        raise SystemExit(message)
    samples = read_samples(arguments.audio, arguments.samples)
    model = load(arguments.model_directory, arguments.cpu_threads)
    out.write(transcribe_samples(model, samples).model_dump_json())


if __name__ == "__main__":
    main(sys.argv[1:])
