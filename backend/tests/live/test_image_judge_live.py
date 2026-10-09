import httpx2
import pytest

from deckly.application.ports import ImageQuery
from deckly.config import load_settings
from deckly.infrastructure.moderation.image_judge import LlmCandidateJudge
from deckly.worker.settings import build_media_judge_llm_client
from tests.commons_recordings import STOP_SIGN_NOTE, STOP_SIGN_SEARCH, STOP_SIGNS
from tests.domain.builders import result_with
from tests.fakes import ManualTime, commons_fetcher, fresh_probe, job_id

pytestmark = [pytest.mark.live, pytest.mark.anyio]

STOP_SIGN_FILES = frozenset(title.removeprefix("File:").replace(" ", "_") for title in STOP_SIGNS)


async def test_configured_judge_keeps_the_letterbox_photo_out_and_picks_a_real_stop_sign() -> None:
    llm = build_media_judge_llm_client(load_settings().providers, probe=fresh_probe())
    fetcher = commons_fetcher(
        lambda _: httpx2.Response(200, json=STOP_SIGN_SEARCH), ManualTime(), judge=LlmCandidateJudge(llm=llm)
    )
    try:
        [attachment] = await fetcher.fetch(
            job_id(1),
            result_with(STOP_SIGN_NOTE),
            (ImageQuery(client_id=STOP_SIGN_NOTE.client_id, text="stop sign"),),
        )
    finally:
        await fetcher.aclose()
        await llm.aclose()

    assert attachment.media.url.split("/")[-2] in STOP_SIGN_FILES
