import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.project import _utcnow


class ExtractionRun(Base):
    """Metadata about one extraction attempt. Never holds prompts, responses or credentials."""

    __tablename__ = "extraction_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "project_id"],
            ["documents.id", "documents.project_id"],
            name="fk_extraction_runs_document_project",
        ),
        CheckConstraint("status IN ('succeeded', 'failed')", name="ck_extraction_runs_status"),
        Index("ix_extraction_runs_document", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Requirement(Base):
    __tablename__ = "requirements"
    __table_args__ = (
        # A requirement can only reference a document of its own project.
        ForeignKeyConstraint(
            ["document_id", "project_id"],
            ["documents.id", "documents.project_id"],
            name="fk_requirements_document_project",
        ),
        CheckConstraint(
            "type IN ('business', 'functional', 'non_functional', 'constraint')",
            name="ck_requirements_type",
        ),
        CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')", name="ck_requirements_status"
        ),
        CheckConstraint(
            "source_start >= 0 AND source_end > source_start "
            "AND source_end - source_start = length(source_quote)",
            name="ck_requirements_source_span",
        ),
        Index("ix_requirements_project_status", "project_id", "status"),
        Index("ix_requirements_document", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("extraction_runs.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_quote: Mapped[str] = mapped_column(Text, nullable=False)
    source_start: Mapped[int] = mapped_column(Integer, nullable=False)
    source_end: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
