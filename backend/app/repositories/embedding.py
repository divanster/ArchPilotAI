import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.chunk_embedding import ChunkEmbedding
from app.models.document import Document
from app.models.document_chunk import DocumentChunk


@dataclass(frozen=True)
class EmbeddingSpace:
    """Vectors are comparable only inside one space."""

    provider: str
    model: str
    dimension: int
    embedder_version: str


@dataclass(frozen=True)
class ChunkText:
    chunk_id: uuid.UUID
    text: str


@dataclass(frozen=True)
class NewEmbedding:
    chunk_id: uuid.UUID
    content_hash: str
    vector: list[float]
    actual_model: str | None


@dataclass(frozen=True)
class VectorRow:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    source_start: int
    source_end: int
    content_hash: str
    vector: list[Any]


def _in_space(space: EmbeddingSpace) -> tuple[Any, ...]:
    return (
        ChunkEmbedding.provider == space.provider,
        ChunkEmbedding.model == space.model,
        ChunkEmbedding.dimension == space.dimension,
        ChunkEmbedding.embedder_version == space.embedder_version,
    )


class EmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def document_chunks(self, project_id: uuid.UUID, document_id: uuid.UUID) -> list[ChunkText]:
        rows = self._session.execute(
            select(DocumentChunk.id, DocumentChunk.text)
            .where(DocumentChunk.project_id == project_id, DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        ).all()
        return [ChunkText(*row) for row in rows]

    def chunk_count(self, project_id: uuid.UUID, document_id: uuid.UUID | None) -> int:
        stmt = select(func.count(DocumentChunk.id)).where(DocumentChunk.project_id == project_id)
        if document_id is not None:
            stmt = stmt.where(DocumentChunk.document_id == document_id)
        return int(self._session.execute(stmt).scalar_one())

    def stored_hashes(
        self, project_id: uuid.UUID, document_id: uuid.UUID, space: EmbeddingSpace
    ) -> dict[uuid.UUID, str]:
        rows = self._session.execute(
            select(ChunkEmbedding.chunk_id, ChunkEmbedding.content_hash)
            .join(
                DocumentChunk,
                (DocumentChunk.id == ChunkEmbedding.chunk_id)
                & (DocumentChunk.project_id == ChunkEmbedding.project_id),
            )
            .where(
                ChunkEmbedding.project_id == project_id,
                DocumentChunk.document_id == document_id,
                *_in_space(space),
            )
        ).all()
        return {chunk_id: digest for chunk_id, digest in rows}

    def replace(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        space: EmbeddingSpace,
        items: Sequence[NewEmbedding],
    ) -> None:
        """Swap the document's vectors in one space atomically; on failure the old ones survive."""
        try:
            document_chunks = select(DocumentChunk.id).where(
                DocumentChunk.project_id == project_id, DocumentChunk.document_id == document_id
            )
            self._session.execute(
                delete(ChunkEmbedding).where(
                    ChunkEmbedding.project_id == project_id,
                    ChunkEmbedding.chunk_id.in_(document_chunks),
                    *_in_space(space),
                )
            )
            self._session.add_all(
                ChunkEmbedding(
                    project_id=project_id,
                    chunk_id=item.chunk_id,
                    provider=space.provider,
                    model=space.model,
                    actual_model=item.actual_model,
                    dimension=space.dimension,
                    embedder_version=space.embedder_version,
                    content_hash=item.content_hash,
                    vector=item.vector,
                )
                for item in items
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def scan(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID | None,
        space: EmbeddingSpace,
        limit: int,
    ) -> list[VectorRow]:
        """Vectors in one space for one project, filtered before any similarity is computed."""
        stmt = (
            select(
                DocumentChunk.id,
                DocumentChunk.document_id,
                Document.original_filename,
                DocumentChunk.chunk_index,
                DocumentChunk.text,
                DocumentChunk.source_start,
                DocumentChunk.source_end,
                ChunkEmbedding.content_hash,
                ChunkEmbedding.vector,
            )
            .join(
                DocumentChunk,
                (DocumentChunk.id == ChunkEmbedding.chunk_id)
                & (DocumentChunk.project_id == ChunkEmbedding.project_id),
            )
            .join(
                Document,
                (Document.id == DocumentChunk.document_id)
                & (Document.project_id == DocumentChunk.project_id),
            )
            .where(ChunkEmbedding.project_id == project_id, *_in_space(space))
            .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
            .limit(limit)
        )
        if document_id is not None:
            stmt = stmt.where(DocumentChunk.document_id == document_id)
        return [VectorRow(*row) for row in self._session.execute(stmt).all()]
