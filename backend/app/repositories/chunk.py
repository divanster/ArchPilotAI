import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.domain.chunking import TextChunk
from app.models.chunk_embedding import ChunkEmbedding
from app.models.document import Document
from app.models.document_chunk import DocumentChunk


@dataclass(frozen=True)
class ChunkState:
    count: int
    versions: frozenset[str]
    indexed_at: datetime | None


@dataclass(frozen=True)
class ChunkRow:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    source_start: int
    source_end: int


class ChunkRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def state(self, project_id: uuid.UUID, document_id: uuid.UUID) -> ChunkState:
        stmt = select(
            func.count(DocumentChunk.id),
            func.max(DocumentChunk.created_at),
        ).where(DocumentChunk.project_id == project_id, DocumentChunk.document_id == document_id)
        count, indexed_at = self._session.execute(stmt).one()
        versions = self._session.scalars(
            select(DocumentChunk.chunker_version)
            .where(DocumentChunk.project_id == project_id, DocumentChunk.document_id == document_id)
            .distinct()
        ).all()
        return ChunkState(int(count), frozenset(versions), indexed_at)

    def replace(
        self,
        project_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: Sequence[TextChunk],
        version: str,
    ) -> None:
        """Swap the document's chunks in one transaction; on any failure the old rows survive."""
        try:
            # Embeddings belong to the chunks being replaced; remove them explicitly as well
            # as by foreign-key cascade. Same transaction as the swap.
            old_chunks = select(DocumentChunk.id).where(
                DocumentChunk.project_id == project_id, DocumentChunk.document_id == document_id
            )
            self._session.execute(
                delete(ChunkEmbedding).where(
                    ChunkEmbedding.project_id == project_id,
                    ChunkEmbedding.chunk_id.in_(old_chunks),
                )
            )
            self._session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.project_id == project_id, DocumentChunk.document_id == document_id
                )
            )
            self._session.add_all(
                DocumentChunk(
                    project_id=project_id,
                    document_id=document_id,
                    chunk_index=c.index,
                    text=c.text,
                    source_start=c.start,
                    source_end=c.end,
                    chunker_version=version,
                )
                for c in chunks
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def unindexed_document_count(self, project_id: uuid.UUID, document_id: uuid.UUID | None) -> int:
        indexed = select(DocumentChunk.document_id).where(DocumentChunk.project_id == project_id)
        stmt = select(func.count(Document.id)).where(
            Document.project_id == project_id, Document.id.not_in(indexed)
        )
        if document_id is not None:
            stmt = stmt.where(Document.id == document_id)
        return int(self._session.execute(stmt).scalar_one())

    def scan(
        self, project_id: uuid.UUID, document_id: uuid.UUID | None, limit: int
    ) -> list[ChunkRow]:
        """In-scope chunks, filtered by project (on both tables) before anything is ranked."""
        stmt = (
            select(
                DocumentChunk.id,
                DocumentChunk.document_id,
                Document.original_filename,
                DocumentChunk.chunk_index,
                DocumentChunk.text,
                DocumentChunk.source_start,
                DocumentChunk.source_end,
            )
            .join(
                Document,
                (Document.id == DocumentChunk.document_id)
                & (Document.project_id == DocumentChunk.project_id),
            )
            .where(DocumentChunk.project_id == project_id)
            .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index)
            .limit(limit)
        )
        if document_id is not None:
            stmt = stmt.where(DocumentChunk.document_id == document_id)
        return [ChunkRow(*row) for row in self._session.execute(stmt).all()]
