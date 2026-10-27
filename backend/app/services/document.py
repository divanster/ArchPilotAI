import logging
import uuid
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError

from app.domain.document import parse_upload
from app.models.document import Document
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)


class DocumentNotFoundError(Exception):
    def __init__(self, document_id: uuid.UUID) -> None:
        super().__init__(f"Document {document_id} not found")
        self.document_id = document_id


class DuplicateDocumentError(Exception):
    def __init__(self, existing_id: uuid.UUID | None = None) -> None:
        suffix = f" as document {existing_id}" if existing_id else ""
        super().__init__(f"Identical content already uploaded{suffix}")


class DocumentService:
    def __init__(
        self, documents: DocumentRepository, projects: ProjectRepository, max_upload_bytes: int
    ) -> None:
        self._documents = documents
        self._projects = projects
        self._max_bytes = max_upload_bytes

    def _require_project(self, project_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)

    def upload(
        self, project_id: uuid.UUID, filename: str | None, content_type: str | None, data: bytes
    ) -> Document:
        self._require_project(project_id)
        parsed = parse_upload(filename, content_type, data, self._max_bytes)

        existing = self._documents.find_by_hash(project_id, parsed.content_sha256)
        if existing is not None:
            raise DuplicateDocumentError(existing.id)

        document = Document(
            project_id=project_id,
            original_filename=parsed.filename,
            media_type=parsed.media_type,
            size_bytes=parsed.size_bytes,
            content_sha256=parsed.content_sha256,
            extracted_text=parsed.text,
        )
        try:
            saved = self._documents.add(document)
        except IntegrityError as exc:
            # Concurrent identical upload won the unique constraint race.
            raise DuplicateDocumentError() from exc
        # Log identifiers and size only; never document content.
        logger.info(
            "document uploaded id=%s project=%s size=%d", saved.id, project_id, saved.size_bytes
        )
        return saved

    def list(self, project_id: uuid.UUID) -> Sequence[Document]:
        self._require_project(project_id)
        return self._documents.list(project_id)

    def get(self, project_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        self._require_project(project_id)
        document = self._documents.get(project_id, document_id)
        if document is None:
            raise DocumentNotFoundError(document_id)
        return document

    def delete(self, project_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self._documents.delete(self.get(project_id, document_id))
