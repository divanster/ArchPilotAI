import uuid
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import RetrievalServiceDep
from app.api.retrieval_schemas import (
    IndexRequest,
    IndexResponse,
    IndexStatusResponse,
    SearchRequest,
    SearchResponse,
    SearchResultResponse,
)
from app.domain.chunking import ChunkingError
from app.domain.search import InvalidQueryError
from app.services.document import DocumentNotFoundError
from app.services.project import ProjectNotFoundError
from app.services.retrieval import IndexConflictError

router = APIRouter(prefix="/api/projects/{project_id}", tags=["retrieval"])


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectNotFoundError | DocumentNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, IndexConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


_HANDLED = (
    ProjectNotFoundError,
    DocumentNotFoundError,
    IndexConflictError,
    ChunkingError,
    InvalidQueryError,
)


@router.post("/documents/{document_id}/index", response_model=IndexResponse)
async def index_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    service: RetrievalServiceDep,
    body: IndexRequest | None = None,
) -> IndexResponse:
    reindex = body.reindex if body else False
    try:
        result = await run_in_threadpool(
            lambda: service.index(project_id, document_id, reindex=reindex)
        )
    except _HANDLED as exc:
        raise _http_error(exc) from exc
    return IndexResponse(action=result.action, **asdict(result.status))


@router.get("/documents/{document_id}/index-status", response_model=IndexStatusResponse)
def index_status(
    project_id: uuid.UUID, document_id: uuid.UUID, service: RetrievalServiceDep
) -> IndexStatusResponse:
    try:
        return IndexStatusResponse(**asdict(service.status(project_id, document_id)))
    except _HANDLED as exc:
        raise _http_error(exc) from exc


@router.post("/search", response_model=SearchResponse)
def search(
    project_id: uuid.UUID, body: SearchRequest, service: RetrievalServiceDep
) -> SearchResponse:
    try:
        outcome = service.search(
            project_id, body.query, document_id=body.document_id, top_k=body.top_k
        )
    except _HANDLED as exc:
        raise _http_error(exc) from exc
    return SearchResponse(
        query_terms=outcome.terms,
        results=[
            SearchResultResponse(**{**asdict(h), "matched_terms": list(h.matched_terms)})
            for h in outcome.hits
        ],
        scanned_chunks=outcome.scanned_chunks,
        unindexed_documents=outcome.unindexed_documents,
        scan_limited=outcome.scan_limited,
    )
