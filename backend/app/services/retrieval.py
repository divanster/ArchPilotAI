import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.exc import IntegrityError

from app.domain.chunking import ChunkSettings, chunk_text
from app.domain.search import MAX_TOP_K, Scorable, parse_query, rank
from app.models.document import Document
from app.repositories.chunk import ChunkRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.services.document import DocumentNotFoundError
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)

IndexState = Literal["not_indexed", "indexed", "stale"]
IndexAction = Literal["indexed", "reindexed", "unchanged"]


class IndexConflictError(Exception):
    def __init__(self) -> None:
        super().__init__("The document is being indexed concurrently; try again")


@dataclass(frozen=True)
class IndexStatus:
    document_id: uuid.UUID
    state: IndexState
    chunk_count: int
    chunker_version: str | None
    current_chunker_version: str
    indexed_at: datetime | None


@dataclass(frozen=True)
class IndexResult:
    action: IndexAction
    status: IndexStatus


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    source_start: int
    source_end: int
    score: float
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class SearchOutcome:
    terms: list[str]
    hits: list[SearchHit]
    scanned_chunks: int
    unindexed_documents: int
    scan_limited: bool


class RetrievalService:
    def __init__(
        self,
        chunks: ChunkRepository,
        documents: DocumentRepository,
        projects: ProjectRepository,
        settings: ChunkSettings,
        scan_limit: int,
    ) -> None:
        self._chunks = chunks
        self._documents = documents
        self._projects = projects
        self._settings = settings
        self._scan_limit = scan_limit

    def _require_document(self, project_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        document = self._documents.get(project_id, document_id)
        if document is None:
            raise DocumentNotFoundError(document_id)
        return document

    def status(self, project_id: uuid.UUID, document_id: uuid.UUID) -> IndexStatus:
        self._require_document(project_id, document_id)
        return self._status(project_id, document_id)

    def _status(self, project_id: uuid.UUID, document_id: uuid.UUID) -> IndexStatus:
        state = self._chunks.state(project_id, document_id)
        current = self._settings.version
        if state.count == 0:
            label: IndexState = "not_indexed"
        elif state.versions == {current}:
            label = "indexed"
        else:
            label = "stale"
        stored = None
        if state.versions:
            stored = current if state.versions == {current} else sorted(state.versions)[0]
        return IndexStatus(document_id, label, state.count, stored, current, state.indexed_at)

    def index(
        self, project_id: uuid.UUID, document_id: uuid.UUID, *, reindex: bool = False
    ) -> IndexResult:
        document = self._require_document(project_id, document_id)
        before = self._status(project_id, document_id)
        if before.state == "indexed" and not reindex:
            return IndexResult("unchanged", before)

        # Chunks are fully built before the database is touched, so a failure here or in the
        # swap leaves the previous index untouched.
        chunks = chunk_text(document.extracted_text, self._settings)
        try:
            self._chunks.replace(project_id, document_id, chunks, self._settings.version)
        except IntegrityError as exc:
            raise IndexConflictError() from exc
        action: IndexAction = "indexed" if before.state == "not_indexed" else "reindexed"
        # Identifiers and counts only; never document text.
        logger.info(
            "document %s id=%s project=%s chunks=%d", action, document_id, project_id, len(chunks)
        )
        return IndexResult(action, self._status(project_id, document_id))

    def check_scope(self, project_id: uuid.UUID, document_id: uuid.UUID | None) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        if document_id is not None and self._documents.get(project_id, document_id) is None:
            raise DocumentNotFoundError(document_id)

    def unindexed_count(self, project_id: uuid.UUID, document_id: uuid.UUID | None) -> int:
        return self._chunks.unindexed_document_count(project_id, document_id)

    def search(
        self,
        project_id: uuid.UUID,
        query: str,
        *,
        document_id: uuid.UUID | None = None,
        top_k: int = 5,
    ) -> SearchOutcome:
        self.check_scope(project_id, document_id)
        terms = parse_query(query)
        top_k = max(1, min(top_k, MAX_TOP_K))

        rows = self._chunks.scan(project_id, document_id, self._scan_limit + 1)
        limited = len(rows) > self._scan_limit
        rows = rows[: self._scan_limit]
        by_id = {str(r.chunk_id): r for r in rows}
        scored = rank(
            [
                Scorable(str(r.chunk_id), r.filename, str(r.document_id), r.chunk_index, r.text)
                for r in rows
            ],
            terms,
            top_k,
        )
        hits = [
            SearchHit(
                chunk_id=by_id[s.item.chunk_id].chunk_id,
                document_id=by_id[s.item.chunk_id].document_id,
                filename=s.item.filename,
                chunk_index=s.item.chunk_index,
                text=s.item.text,
                source_start=by_id[s.item.chunk_id].source_start,
                source_end=by_id[s.item.chunk_id].source_end,
                score=s.score,
                matched_terms=s.matched_terms,
            )
            for s in scored
        ]
        unindexed = self._chunks.unindexed_document_count(project_id, document_id)
        logger.info(
            "search project=%s terms=%d scanned=%d hits=%d",
            project_id,
            len(terms),
            len(rows),
            len(hits),
        )
        return SearchOutcome(terms, hits, len(rows), unindexed, limited)
