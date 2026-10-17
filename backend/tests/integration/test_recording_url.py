import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.documents import FetchRecording
from deckly.application.exceptions import UpstreamUnavailableError
from deckly.config import Settings
from deckly.domain.document import DocumentFormat
from deckly.infrastructure.documents.store import DocumentStatus
from deckly.infrastructure.quota import address_bucket
from deckly.infrastructure.recording_url.egress import EgressProxy, EgressRefusal
from deckly.infrastructure.recording_url.fetcher import (
    YOUTUBE_EGRESS_POLICY,
    EgressRefusedError,
    FetchLimits,
    YtDlpRecordingFetcher,
    egress_limits,
)
from deckly.infrastructure.recording_url.ytdlp import YtDlpInvocation, bundled_deno, require_runtime
from deckly.infrastructure.recordings.sniffing import detect_recording
from deckly.infrastructure.tables import DocumentRow
from deckly.infrastructure.upload_limit import (
    RECORDING_URL_ADDRESS_KEY_PREFIX,
    RECORDING_URL_CLIENT_KEY_PREFIX,
    RECORDING_URL_SERVER_KEY_PREFIX,
)
from deckly.main import API_PREFIX, create_app
from tests.fake_ytdlp import VIDEO, FakeYtDlp, fake_fetcher, video_info
from tests.fakes import fresh_metrics
from tests.ffmpeg import extract_audio
from tests.integration.conftest import TEST_PEER_ADDRESSES, open_queue_pool
from tests.integration.storage import ObjectBucket
from tests.recording_files import FIXTURES
from tests.recording_url import PUBLIC_ADDRESS, LoopbackConnector, ScriptedResolver, listening
from tests.reserved_clients import reserved_client_id
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

FROM_URL = f"{API_PREFIX}/recordings/from-url"
DOCUMENTS = f"{API_PREFIX}/documents"
LINK = "https://youtu.be/jNQXAC9IVRw"
RECORDING = FIXTURES / "tone-fragmented.m4a"
FETCH_LIMITS = FetchLimits(max_bytes=8 * 1024 * 1024, max_duration_seconds=900, timeout_seconds=60)
EXTRACTION_CAP_SECONDS = 3
YOUTUBE_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com")


class FetchCleanup:
    def __init__(
        self, settings: Settings, session_factory: async_sessionmaker[AsyncSession], bucket: ObjectBucket
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.bucket = bucket
        self.client_ids: set[UUID] = set()

    def client_id(self) -> UUID:
        client_id = reserved_client_id()
        self.client_ids.add(client_id)
        return client_id

    async def purge(self) -> None:
        async with self.session_factory.begin() as session:
            removed = await session.scalars(
                delete(DocumentRow)
                .where(DocumentRow.client_id.in_(self.client_ids))
                .returning(DocumentRow.storage_key)
            )
            keys = removed.all()
        for key in keys:
            await self.bucket.remove(key)
        pool = await open_queue_pool(self.settings)
        try:
            patterns = [f"{RECORDING_URL_CLIENT_KEY_PREFIX}:{client_id}:*" for client_id in self.client_ids]
            patterns += [
                f"{RECORDING_URL_ADDRESS_KEY_PREFIX}:{address_bucket(address)}:*"
                for address in TEST_PEER_ADDRESSES
            ]
            patterns.append(f"{RECORDING_URL_SERVER_KEY_PREFIX}:*")
            for pattern in patterns:
                async for key in pool.scan_iter(match=pattern):
                    await pool.delete(key)
        finally:
            await pool.aclose()


@pytest.fixture
async def fetched(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[FetchCleanup]:
    bucket = ObjectBucket(settings)
    await bucket.ensure()
    tracker = FetchCleanup(settings, session_factory, bucket)
    try:
        yield tracker
    finally:
        await tracker.purge()


@contextmanager
def app_with_fetcher(settings: Settings, fake: FakeYtDlp) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        app = create_app(settings)
        with TestClient(app) as client:
            wired = app.state.fetch_recording
            assert isinstance(wired, FetchRecording)
            app.state.fetch_recording = replace(
                wired, fetcher=fake_fetcher(fake, max_bytes=FETCH_LIMITS.max_bytes)
            )
            yield client
    finally:
        root.handlers, root.level = handlers, level


async def stored_row(
    session_factory: async_sessionmaker[AsyncSession], document_id: str
) -> DocumentRow | None:
    async with session_factory() as session:
        return await session.get(DocumentRow, UUID(document_id))


async def test_fetched_recording_is_stored_with_a_row_and_an_object_exactly_like_an_upload(
    settings: Settings, fetched: FetchCleanup, tmp_path: Path
) -> None:
    client_id = fetched.client_id()
    fake = FakeYtDlp(tmp_path, {"info": video_info()}, {"file": str(RECORDING)})
    with app_with_fetcher(settings, fake) as client:
        response = client.post(FROM_URL, json={"url": LINK}, headers={"X-Client-Id": str(client_id)})
        body = response.json()
        read = client.get(f"{DOCUMENTS}/{body['documentId']}", headers={"X-Client-Id": str(client_id)})

    assert response.status_code == HTTPStatus.CREATED, body
    assert spec_errors("Document", body) == []
    row = await stored_row(fetched.session_factory, body["documentId"])
    assert row is not None
    assert row.status == DocumentStatus.STORED
    assert row.content_type == DocumentFormat.M4A
    assert row.duration_seconds == body["durationSeconds"] == pytest.approx(2.064)
    assert row.filename == "Lecture 1: Introduction.m4a"
    assert row.storage_key == f"documents/{body['documentId']}"
    assert await fetched.bucket.read(row.storage_key) == RECORDING.read_bytes()
    assert read.status_code == HTTPStatus.OK
    assert read.json() == body


async def test_stored_fragmented_recording_yields_its_audio_through_the_hardened_ffmpeg_extraction(
    tmp_path: Path,
) -> None:
    output = tmp_path / "audio.wav"

    await extract_audio(RECORDING, DocumentFormat.M4A, EXTRACTION_CAP_SECONDS, output)

    extracted = detect_recording(output.read_bytes(), EXTRACTION_CAP_SECONDS)
    assert extracted.duration_seconds == pytest.approx(
        detect_recording(RECORDING.read_bytes(), 900).duration_seconds, abs=0.1
    )


async def test_fetch_budget_is_spent_per_request_and_reported_as_429(
    settings: Settings, fetched: FetchCleanup, tmp_path: Path
) -> None:
    limited = replace(
        settings,
        limits=settings.limits.model_copy(
            update={"recording_url_fetches_per_window": 1, "recording_url_fetches_per_address_per_window": 20}
        ),
    )
    client_id = fetched.client_id()
    fake = FakeYtDlp(tmp_path, {"info": video_info(duration=10_000)}, {"file": str(RECORDING)})
    with app_with_fetcher(limited, fake) as client:
        first = client.post(FROM_URL, json={"url": LINK}, headers={"X-Client-Id": str(client_id)})
        second = client.post(FROM_URL, json={"url": LINK}, headers={"X-Client-Id": str(client_id)})
        unsupported = client.post(
            FROM_URL, json={"url": "https://vimeo.com/1"}, headers={"X-Client-Id": str(client_id)}
        )

    assert first.json()["code"] == "RECORDING_TOO_LONG"
    assert second.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert unsupported.json()["code"] == "UNSUPPORTED_RECORDING_URL"
    assert len(fake.invocations) == 1


def real_fetcher(resolver: ScriptedResolver, connector: LoopbackConnector) -> YtDlpRecordingFetcher:
    deno = bundled_deno()
    require_runtime(deno)
    metrics = fresh_metrics()
    return YtDlpRecordingFetcher(
        invocation=YtDlpInvocation(deno_path=deno, socket_timeout_seconds=5),
        proxy=EgressProxy(
            YOUTUBE_EGRESS_POLICY,
            egress_limits(FETCH_LIMITS.max_bytes),
            metrics,
            resolver=resolver,
            connector=connector,
        ),
        limits=FETCH_LIMITS,
        metrics=metrics,
    )


def poisoned_dns(address: str) -> ScriptedResolver:
    return ScriptedResolver({host: [[address]] for host in YOUTUBE_HOSTS})


@pytest.mark.parametrize("internal", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"])
async def test_real_yt_dlp_cannot_reach_an_internal_address_even_when_youtube_resolves_to_one(
    internal: str,
) -> None:
    async with listening() as service:
        connector = LoopbackConnector(service.port)
        fetcher = real_fetcher(poisoned_dns(internal), connector)

        with pytest.raises(EgressRefusedError):
            await fetcher.fetch(VIDEO)

    assert connector.dialled == []
    assert service.connections == 0
    assert fetcher.metrics.registry.get_sample_value(
        "deckly_recording_url_egress_refusals_total", {"reason": EgressRefusal.ADDRESS}
    )


async def test_positive_control_real_yt_dlp_does_go_through_the_proxy_to_the_checked_address() -> None:
    async with listening(reply=b"not tls") as service:
        connector = LoopbackConnector(service.port)
        fetcher = real_fetcher(poisoned_dns(PUBLIC_ADDRESS), connector)

        with pytest.raises(UpstreamUnavailableError) as raised:
            await fetcher.fetch(VIDEO)

    assert not isinstance(raised.value, EgressRefusedError)
    assert connector.dialled
    assert set(connector.dialled) == {PUBLIC_ADDRESS}
    assert service.connections >= 1
