import hashlib
import re
from pathlib import Path

import pytest

from deckly.infrastructure.transcription import model
from deckly.infrastructure.transcription.model import (
    parse_checksums,
    pinned_checksums,
    require_ffmpeg,
    require_model,
)

MAKEFILE = Path(__file__).parents[2] / "Makefile"
SHA256 = re.compile(r"[0-9a-f]{64}")
MODEL_FILES = {"config.json", "model.bin", "tokenizer.json", "vocabulary.txt"}


def model_directory(tmp_path: Path, contents: dict[str, bytes]) -> tuple[Path, dict[str, str]]:
    directory = tmp_path / "model"
    directory.mkdir()
    for name, content in contents.items():
        (directory / name).write_bytes(content)
    return directory, {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}


def test_pinned_checksums_cover_every_file_the_engine_loads() -> None:
    checksums = pinned_checksums()

    assert set(checksums) == MODEL_FILES
    assert all(SHA256.fullmatch(digest) for digest in checksums.values())


def test_make_model_verifies_against_the_same_checksum_file_the_worker_reads() -> None:
    makefile = MAKEFILE.read_text()

    assert "src/deckly/infrastructure/transcription/model.sha256" in makefile
    assert "shasum -a 256 --check" in makefile


def test_checksum_lines_are_read_as_digest_then_name_and_blank_lines_ignored() -> None:
    checksums = parse_checksums(f"{'a' * 64}  config.json\n\n{'b' * 64}  model.bin\n")

    assert dict(checksums) == {"config.json": "a" * 64, "model.bin": "b" * 64}


def test_model_whose_every_file_matches_is_accepted(tmp_path: Path) -> None:
    directory, checksums = model_directory(tmp_path, {"model.bin": b"weights", "config.json": b"{}"})

    require_model(directory, checksums)


def test_model_missing_a_file_is_refused_with_the_command_that_fetches_it(tmp_path: Path) -> None:
    directory, checksums = model_directory(tmp_path, {"config.json": b"{}"})

    with pytest.raises(RuntimeError, match=r"no model\.bin .*run make model"):
        require_model(directory, {**checksums, "model.bin": "0" * 64})


def test_model_file_that_is_not_the_pinned_one_is_refused(tmp_path: Path) -> None:
    directory, checksums = model_directory(tmp_path, {"model.bin": b"weights"})
    (directory / "model.bin").write_bytes(b"weights from another revision")

    with pytest.raises(RuntimeError, match=r"model\.bin .* is not the pinned one"):
        require_model(directory, checksums)


def test_missing_ffmpeg_is_refused_with_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "which", lambda _: None)

    with pytest.raises(RuntimeError, match="apt-get install ffmpeg"):
        require_ffmpeg()


def test_ffmpeg_is_found_by_its_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "which", lambda _: "/usr/bin/ffmpeg")

    assert require_ffmpeg() == Path("/usr/bin/ffmpeg")
