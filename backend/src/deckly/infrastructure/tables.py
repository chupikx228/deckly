from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Double, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from deckly.infrastructure.database import Base


class GenerationJobRow(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_generation_jobs_client_id_idempotency_key"),
    )

    job_id: Mapped[UUID] = mapped_column(primary_key=True)
    client_id: Mapped[UUID]
    idempotency_key: Mapped[UUID]
    topic: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    card_count: Mapped[int]
    difficulty: Mapped[str] = mapped_column(Text)
    note_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    include_images: Mapped[bool]
    instructions: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[float] = mapped_column(Double)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


metadata = Base.metadata
