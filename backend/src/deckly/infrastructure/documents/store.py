import logging
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Select, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deckly.application.ports import DocumentStore, ExpiredDocument
from deckly.domain.document import Document, DocumentFormat
from deckly.infrastructure.bounded import BoundedCalls
from deckly.infrastructure.tables import DocumentRow

logger = logging.getLogger(__name__)


class DocumentStatus(StrEnum):
    PENDING = "pending"
    STORED = "stored"


class CorruptStoredDocumentError(Exception):
    pass


class DocumentStoreTimeoutError(TimeoutError):
    pass


def restore_document(row: DocumentRow) -> Document:
    try:
        document_format = DocumentFormat(row.content_type)
    except ValueError as error:
        message = f"document {row.document_id} has an unknown content type"
        raise CorruptStoredDocumentError(message) from error
    return Document(
        document_id=row.document_id,
        client_id=row.client_id,
        filename=row.filename,
        size_bytes=row.size_bytes,
        document_format=document_format,
        uploaded_at=row.uploaded_at,
    )


class PostgresDocumentStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add_pending(self, document: Document) -> None:
        async with self._session_factory.begin() as session:
            session.add(
                DocumentRow(
                    document_id=document.document_id,
                    client_id=document.client_id,
                    storage_key=document.storage_key,
                    filename=document.filename,
                    size_bytes=document.size_bytes,
                    content_type=document.document_format,
                    status=DocumentStatus.PENDING,
                    uploaded_at=document.uploaded_at,
                    used_at=None,
                )
            )

    async def mark_stored(self, document_id: UUID) -> None:
        statement = (
            update(DocumentRow)
            .where(DocumentRow.document_id == document_id, DocumentRow.status == DocumentStatus.PENDING)
            .values(status=DocumentStatus.STORED)
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def discard(self, document_id: UUID) -> None:
        statement = delete(DocumentRow).where(
            DocumentRow.document_id == document_id, DocumentRow.status == DocumentStatus.PENDING
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def get(self, document_id: UUID, client_id: UUID) -> Document | None:
        statement = select(DocumentRow).where(
            DocumentRow.document_id == document_id,
            DocumentRow.client_id == client_id,
            DocumentRow.status == DocumentStatus.STORED,
        )
        async with self._session_factory() as session:
            row = await session.scalar(statement)
        return None if row is None else restore_document(row)


class BoundedDocumentStore:
    def __init__(self, store: DocumentStore, *, timeout_seconds: float) -> None:
        self._store = store
        self._calls = BoundedCalls(
            timeout_seconds=timeout_seconds,
            subject="document_store",
            timeout_error=DocumentStoreTimeoutError,
            logger=logger,
        )

    async def add_pending(self, document: Document) -> None:
        await self._calls.run(self._store.add_pending(document))

    async def mark_stored(self, document_id: UUID) -> None:
        await self._calls.run(self._store.mark_stored(document_id))

    async def discard(self, document_id: UUID) -> None:
        await self._calls.run(self._store.discard(document_id))

    async def get(self, document_id: UUID, client_id: UUID) -> Document | None:
        return await self._calls.run(self._store.get(document_id, client_id))


def expired_documents_statement(uploaded_before: datetime, limit: int) -> Select[tuple[UUID, str]]:
    return (
        select(DocumentRow.document_id, DocumentRow.storage_key)
        .where(DocumentRow.used_at.is_(None), DocumentRow.uploaded_at < uploaded_before)
        .order_by(DocumentRow.uploaded_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


@dataclass(frozen=True, slots=True)
class PostgresExpiredDocuments:
    session: AsyncSession
    documents: tuple[ExpiredDocument, ...]

    async def purge(self, document_ids: Collection[UUID]) -> int:
        if not document_ids:
            return 0
        statement = (
            delete(DocumentRow)
            .where(DocumentRow.document_id.in_(tuple(document_ids)), DocumentRow.used_at.is_(None))
            .returning(DocumentRow.document_id)
        )
        return len((await self.session.scalars(statement)).all())


class PostgresDocumentHousekeeping:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def expired(self, uploaded_before: datetime, limit: int) -> AsyncIterator[PostgresExpiredDocuments]:
        async with self._session_factory.begin() as session:
            rows = (await session.execute(expired_documents_statement(uploaded_before, limit))).all()
            yield PostgresExpiredDocuments(
                session=session,
                documents=tuple(
                    ExpiredDocument(document_id=document_id, storage_key=key) for document_id, key in rows
                ),
            )
