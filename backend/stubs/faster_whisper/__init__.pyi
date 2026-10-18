from collections.abc import Iterable

import numpy as np
from numpy.typing import NDArray

class Segment:
    @property
    def start(self) -> float: ...
    @property
    def end(self) -> float: ...
    @property
    def text(self) -> str: ...

class TranscriptionInfo:
    @property
    def language(self) -> str: ...

class WhisperModel:
    def __init__(
        self,
        model_size_or_path: str,
        device: str = ...,
        compute_type: str = ...,
        cpu_threads: int = ...,
        num_workers: int = ...,
        local_files_only: bool = ...,
    ) -> None: ...
    def transcribe(
        self,
        audio: NDArray[np.float32],
        language: str | None = ...,
        beam_size: int = ...,
        vad_filter: bool = ...,
    ) -> tuple[Iterable[Segment], TranscriptionInfo]: ...
