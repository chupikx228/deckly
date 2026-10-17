from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from deckly.infrastructure.database import Base


class GenerationJobRow(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_generation_jobs_client_id_idempotency_key"),
        Index("ix_generation_jobs_status_updated_at", "status", "updated_at"),
        Index(
            "ix_generation_jobs_document_id",
            "document_id",
            postgresql_where=text("document_id IS NOT NULL"),
        ),
        CheckConstraint("num_nonnulls(topic, document_id) = 1", name="one_source"),
    )

    job_id: Mapped[UUID] = mapped_column(primary_key=True)
    client_id: Mapped[UUID]
    idempotency_key: Mapped[UUID | None]
    topic: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.document_id", ondelete="RESTRICT"))
    language: Mapped[str] = mapped_column(Text)
    card_count: Mapped[int]
    difficulty: Mapped[str] = mapped_column(Text)
    note_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    include_images: Mapped[bool]
    instructions: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[float] = mapped_column(Double)
    failure_code: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_documents_storage_key"),
        Index("ix_documents_uploaded_at", "uploaded_at"),
    )

    document_id: Mapped[UUID] = mapped_column(primary_key=True)
    client_id: Mapped[UUID]
    storage_key: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


metadata = Base.metadata
