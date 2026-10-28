import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.project import _utcnow


class ChunkEmbedding(Base):
    """A vector for one chunk in one embedding space.

    The space is ``(provider, model, dimension, embedder_version)``; a row is fresh only while
    ``content_hash`` equals the SHA-256 of the chunk's current text. Vectors are stored as JSON
    so SQLite and PostgreSQL behave identically; a pgvector adapter would replace only the
    ``vector`` column and the scan query. Derived data: safe to delete and rebuild.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        # Ties the vector to the chunk's own project, so it can never cross projects.
        ForeignKeyConstraint(
            ["chunk_id", "project_id"],
            ["document_chunks.id", "document_chunks.project_id"],
            name="fk_chunk_embeddings_chunk_project",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "chunk_id",
            "provider",
            "model",
            "dimension",
            "embedder_version",
            name="uq_chunk_embeddings_space",
        ),
        CheckConstraint("dimension > 0", name="ck_chunk_embeddings_dimension"),
        Index(
            "ix_chunk_embeddings_space",
            "project_id",
            "provider",
            "model",
            "dimension",
            "embedder_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    actual_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedder_version: Mapped[str] = mapped_column(String(50), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vector: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
