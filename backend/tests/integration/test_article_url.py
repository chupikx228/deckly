import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from http import HTTPStatus
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.documents import FetchArticle
from deckly.config import Settings
from deckly.domain.document import DocumentFormat
from deckly.infrastructure.article_url.extraction import ExtractorInvocation, SubprocessArticleExtractor
from deckly.infrastructure.article_url.fetcher import HttpArticleFetcher
from deckly.infrastructure.documents.store import DocumentStatus
from deckly.infrastructure.quota import address_bucket
from deckly.infrastructure.recording_url.egress import EgressRefusal
from deckly.infrastructure.tables import DocumentRow
from deckly.infrastructure.upload_limit import (
    ARTICLE_URL_ADDRESS_KEY_PREFIX,
    ARTICLE_URL_CLIENT_KEY_PREFIX,
    ARTICLE_URL_SERVER_KEY_PREFIX,
)
from deckly.main import API_PREFIX, create_app
from tests.article_url import (
    ARTICLE_HOST,
    ARTICLE_PARAGRAPHS,
    ARTICLE_TITLE,
    METADATA_ADDRESS,
    METADATA_HOST,
    TEST_LIMITS,
    RoutingConnector,
    Site,
    TlsMaterial,
    article_bytes,
    article_fetcher,
    page,
    public_dns,
    redirect,
    resolving_hosts,
    serving,
)
from tests.integration.conftest import TEST_PEER_ADDRESSES, open_queue_pool
from tests.integration.storage import ObjectBucket
from tests.recording_url import PUBLIC_ADDRESS, ScriptedResolver, listening
from tests.reserved_clients import reserved_client_id
from tests.transport.openapi import spec_errors

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

FROM_URL = f"{API_PREFIX}/documents/from-url"
DOCUMENTS = f"{API_PREFIX}/documents"
ARTICLE_PATH = "/science/spaced-repetition"
LINK = f"https://{ARTICLE_HOST}{ARTICLE_PATH}"


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

    def headers(self) -> dict[str, str]:
        return {"X-Client-Id": str(self.client_id())}

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
            patterns = [f"{ARTICLE_URL_CLIENT_KEY_PREFIX}:{client_id}:*" for client_id in self.client_ids]
            patterns += [
                f"{ARTICLE_URL_ADDRESS_KEY_PREFIX}:{address_bucket(address)}:*"
                for address in TEST_PEER_ADDRESSES
            ]
            patterns.append(f"{ARTICLE_URL_SERVER_KEY_PREFIX}:*")
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


def with_real_extractor(fetcher: HttpArticleFetcher) -> HttpArticleFetcher:
    invocation = ExtractorInvocation(max_characters=TEST_LIMITS.max_characters + 1)
    return replace(fetcher, extractor=SubprocessArticleExtractor(invocation, deadline_seconds=30))


@contextmanager
def app_with_fetcher(settings: Settings, fetcher: HttpArticleFetcher) -> Iterator[TestClient]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        app = create_app(settings)
        with TestClient(app) as client:
            wired = app.state.fetch_article
            assert isinstance(wired, FetchArticle)
            app.state.fetch_article = replace(wired, fetcher=fetcher)
            yield client
    finally:
        root.handlers, root.level = handlers, level


async def stored_rows(fetched: FetchCleanup) -> list[DocumentRow]:
    async with fetched.session_factory() as session:
        rows = await session.scalars(select(DocumentRow).where(DocumentRow.client_id.in_(fetched.client_ids)))
        return list(rows.all())


def site_with_article(site: Site) -> None:
    site.serve(ARTICLE_HOST, ARTICLE_PATH, page(article_bytes()))


async def test_fetched_page_is_stored_as_text_with_a_row_an_object_and_its_source_url(
    settings: Settings, fetched: FetchCleanup, tls_material: TlsMaterial
) -> None:
    headers = fetched.headers()
    async with serving(tls_material) as site:
        site_with_article(site)
        connector = RoutingConnector({PUBLIC_ADDRESS: site.port})
        fetcher = with_real_extractor(article_fetcher(tls_material, public_dns(ARTICLE_HOST), connector))
        with app_with_fetcher(settings, fetcher) as client:
            response = client.post(
                FROM_URL, json={"url": f"http://{ARTICLE_HOST}{ARTICLE_PATH}"}, headers=headers
            )
            body = response.json()
            read = client.get(f"{DOCUMENTS}/{body['documentId']}", headers=headers)

    assert response.status_code == HTTPStatus.CREATED, body
    assert spec_errors("Document", body) == []
    assert body["contentType"] == DocumentFormat.TEXT
    assert body["sourceUrl"] == LINK
    assert body["filename"] == ARTICLE_TITLE
    async with fetched.session_factory() as session:
        row = await session.get(DocumentRow, UUID(body["documentId"]))
    assert row is not None
    assert row.status == DocumentStatus.STORED
    assert row.content_type == DocumentFormat.TEXT
    assert row.source_url == LINK
    assert row.duration_seconds is None
    stored = (await fetched.bucket.read(row.storage_key)).decode()
    assert all(paragraph in stored for paragraph in ARTICLE_PARAGRAPHS)
    assert "Sign in to your account" not in stored
    assert row.size_bytes == body["sizeBytes"] == len(stored.encode())
    assert read.status_code == HTTPStatus.OK
    assert read.json() == body


@pytest.mark.parametrize("internal", ["127.0.0.1", METADATA_ADDRESS, "10.0.0.1", "::1"])
async def test_page_whose_host_resolves_internally_is_refused_through_the_whole_app_and_never_reached(
    settings: Settings, fetched: FetchCleanup, tls_material: TlsMaterial, internal: str
) -> None:
    async with listening() as service:
        connector = RoutingConnector({internal: service.port})
        fetcher = article_fetcher(tls_material, resolving_hosts({ARTICLE_HOST: [[internal]]}), connector)
        with app_with_fetcher(settings, fetcher) as client:
            response = client.post(FROM_URL, json={"url": LINK}, headers=fetched.headers())

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["code"] == "ARTICLE_UNAVAILABLE"
    assert connector.dialled == []
    assert service.connections == 0
    assert await stored_rows(fetched) == []
    assert fetcher.metrics.registry.get_sample_value(
        "deckly_article_url_egress_refusals_total", {"reason": EgressRefusal.ADDRESS}
    )


async def test_redirect_to_the_metadata_endpoint_is_refused_through_the_whole_app(
    settings: Settings, fetched: FetchCleanup, tls_material: TlsMaterial
) -> None:
    async with listening() as metadata, serving(tls_material) as site:
        site.serve(ARTICLE_HOST, ARTICLE_PATH, redirect(f"https://{METADATA_HOST}/latest/meta-data/"))
        resolver = resolving_hosts({ARTICLE_HOST: [[PUBLIC_ADDRESS]], METADATA_HOST: [[METADATA_ADDRESS]]})
        connector = RoutingConnector({PUBLIC_ADDRESS: site.port, METADATA_ADDRESS: metadata.port})
        fetcher = article_fetcher(tls_material, resolver, connector)
        with app_with_fetcher(settings, fetcher) as client:
            response = client.post(FROM_URL, json={"url": LINK}, headers=fetched.headers())

    assert response.json()["code"] == "ARTICLE_UNAVAILABLE"
    assert metadata.connections == 0
    assert connector.dialled == [PUBLIC_ADDRESS]
    assert await stored_rows(fetched) == []


async def test_article_budget_is_spent_per_request_and_reported_as_429(
    settings: Settings, fetched: FetchCleanup, tls_material: TlsMaterial
) -> None:
    limited = replace(
        settings,
        limits=settings.limits.model_copy(
            update={"article_url_fetches_per_window": 1, "article_url_fetches_per_address_per_window": 30}
        ),
    )
    headers = fetched.headers()
    resolver = ScriptedResolver({})
    fetcher = article_fetcher(tls_material, resolver, RoutingConnector({}))
    with app_with_fetcher(limited, fetcher) as client:
        first = client.post(FROM_URL, json={"url": LINK}, headers=headers)
        second = client.post(FROM_URL, json={"url": LINK}, headers=headers)
        unsupported = client.post(FROM_URL, json={"url": "https://localhost/"}, headers=headers)

    assert first.json()["code"] == "ARTICLE_UNAVAILABLE"
    assert second.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert unsupported.json()["code"] == "UNSUPPORTED_ARTICLE_URL"
    assert resolver.calls == [ARTICLE_HOST]


async def test_wired_app_builds_the_production_article_fetcher(settings: Settings) -> None:
    with app_with_fetcher_untouched(settings) as wired:
        assert isinstance(wired.fetcher, HttpArticleFetcher)
        assert isinstance(wired.fetcher.extractor, SubprocessArticleExtractor)
        assert wired.fetcher.verify is True


@contextmanager
def app_with_fetcher_untouched(settings: Settings) -> Iterator[FetchArticle]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        app = create_app(settings)
        with TestClient(app):
            wired = app.state.fetch_article
            assert isinstance(wired, FetchArticle)
            yield wired
    finally:
        root.handlers, root.level = handlers, level
