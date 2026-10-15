import os
from pathlib import Path

import pytest

LOCAL_ENVIRONMENT_MARKERS = ("integration", "live")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_from_local_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if any(request.node.get_closest_marker(marker) for marker in LOCAL_ENVIRONMENT_MARKERS):
        return
    monkeypatch.chdir(tmp_path)
    for name in [name for name in os.environ if name.startswith("DECKLY_")]:
        monkeypatch.delenv(name)
