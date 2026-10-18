import hashlib
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from shutil import which
from types import MappingProxyType

CHECKSUMS_RESOURCE = "model.sha256"
DIGEST = "sha256"
FFMPEG = "ffmpeg"


def parse_checksums(text: str) -> Mapping[str, str]:
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            checksums[name.strip()] = digest
    return MappingProxyType(checksums)


def pinned_checksums() -> Mapping[str, str]:
    return parse_checksums((files(__package__) / CHECKSUMS_RESOURCE).read_text(encoding="utf-8"))


def file_digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, DIGEST).hexdigest()


def require_model(directory: Path, checksums: Mapping[str, str]) -> None:
    for name, expected in checksums.items():
        path = directory / name
        if not path.is_file():
            message = f"the transcription model has no {name} in {directory}; run make model"
            raise RuntimeError(message)
        if file_digest(path) != expected:
            message = f"the transcription model's {name} in {directory} is not the pinned one; run make model"
            raise RuntimeError(message)


def require_ffmpeg() -> Path:
    binary = which(FFMPEG)
    if binary is None:
        message = "transcription needs ffmpeg on PATH: apt-get install ffmpeg, or brew install ffmpeg"
        raise RuntimeError(message)
    return Path(binary)
