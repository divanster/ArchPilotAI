import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, document: Document) -> Document:
        self._session.add(document)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(document)
        return document

    def get(self, project_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        stmt = select(Document).where(Document.project_id == project_id, Document.id == document_id)
        return self._session.scalars(stmt).one_or_none()

    def find_by_hash(self, project_id: uuid.UUID, content_sha256: str) -> Document | None:
        stmt = select(Document).where(
            Document.project_id == project_id, Document.content_sha256 == content_sha256
        )
        return self._session.scalars(stmt).one_or_none()

    def list(self, project_id: uuid.UUID) -> Sequence[Document]:
        stmt = (
            select(Document)
            .where(Document.project_id == project_id)
            .order_by(Document.created_at.desc(), Document.id)
        )
        return self._session.scalars(stmt).all()

    def delete(self, document: Document) -> None:
        self._session.delete(document)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
