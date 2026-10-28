import uuid
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import EmbeddingServiceDep, RetrievalServiceDep, SearchServiceDep
from app.api.retrieval_schemas import (
    CapabilitiesResponse,
    EmbeddingStatusResponse,
    EmbedRequest,
    EmbedResponse,
    IndexRequest,
    IndexResponse,
    IndexStatusResponse,
    ProviderIdentityResponse,
    SearchRequest,
    SearchResponse,
    SearchResultResponse,
)
from app.domain.chunking import ChunkingError
from app.domain.search import InvalidQueryError
from app.embeddings.contracts import EmbeddingError
from app.services.document import DocumentNotFoundError
from app.services.embedding import (
    DocumentNotIndexedError,
    EmbeddingConflictError,
    EmbeddingUnavailableError,
)
from app.services.project import ProjectNotFoundError
from app.services.retrieval import IndexConflictError

router = APIRouter(prefix="/api/projects/{project_id}", tags=["retrieval"])


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectNotFoundError | DocumentNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, EmbeddingUnavailableError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    if isinstance(exc, EmbeddingError):
        # Provider messages are never forwarded.
        return HTTPException(status.HTTP_502_BAD_GATEWAY, "The embedding provider failed")
    if isinstance(exc, IndexConflictError | EmbeddingConflictError | DocumentNotIndexedError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


_HANDLED = (
    ProjectNotFoundError,
    DocumentNotFoundError,
    IndexConflictError,
    ChunkingError,
    InvalidQueryError,
    EmbeddingError,
    EmbeddingUnavailableError,
    EmbeddingConflictError,
    DocumentNotIndexedError,
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
def search(project_id: uuid.UUID, body: SearchRequest, service: SearchServiceDep) -> SearchResponse:
    try:
        outcome = service.search(
            project_id,
            body.query,
            mode=body.mode,
            document_id=body.document_id,
            top_k=body.top_k,
        )
    except _HANDLED as exc:
        raise _http_error(exc) from exc
    results = []
    for ranked in outcome.hits:
        fields = asdict(ranked.hit)
        fields["matched_terms"] = list(ranked.hit.matched_terms)
        fields["score"] = ranked.score
        results.append(
            SearchResultResponse(
                **fields,
                bm25_rank=ranked.bm25_rank,
                bm25_score=ranked.bm25_score,
                vector_rank=ranked.vector_rank,
                vector_score=ranked.vector_score,
            )
        )
    return SearchResponse(
        query_terms=outcome.terms,
        results=results,
        scanned_chunks=outcome.scanned_chunks,
        unindexed_documents=outcome.unindexed_documents,
        scan_limited=outcome.scan_limited,
        requested_mode=outcome.requested_mode,
        effective_mode=outcome.effective_mode,
        warnings=outcome.warnings,
        vector_scanned=outcome.vector_scanned,
        embedding=_identity(outcome.identity),
    )


def _identity(identity: object) -> ProviderIdentityResponse | None:
    return ProviderIdentityResponse(**asdict(identity)) if identity is not None else None  # type: ignore[call-overload]


@router.get("/retrieval/capabilities", response_model=CapabilitiesResponse)
def capabilities(project_id: uuid.UUID, service: EmbeddingServiceDep) -> CapabilitiesResponse:
    identity = service.identity()
    available = identity is not None
    return CapabilitiesResponse(
        modes=["keyword", "vector", "hybrid"] if available else ["keyword"],
        vector_available=available,
        vector_unavailable_reason=None if available else "No embedding provider is configured.",
        embedding=_identity(identity),
    )


@router.post("/documents/{document_id}/embed", response_model=EmbedResponse)
async def embed_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    service: EmbeddingServiceDep,
    body: EmbedRequest | None = None,
) -> EmbedResponse:
    reembed = body.reembed if body else False
    try:
        result = await run_in_threadpool(
            lambda: service.embed(project_id, document_id, reembed=reembed)
        )
    except _HANDLED as exc:
        raise _http_error(exc) from exc
    return EmbedResponse(
        action=result.action,
        input_tokens=result.usage.input_tokens if result.usage else None,
        **_status_fields(result.status),
    )


@router.get("/documents/{document_id}/embedding-status", response_model=EmbeddingStatusResponse)
def embedding_status(
    project_id: uuid.UUID, document_id: uuid.UUID, service: EmbeddingServiceDep
) -> EmbeddingStatusResponse:
    try:
        return EmbeddingStatusResponse(**_status_fields(service.status(project_id, document_id)))
    except _HANDLED as exc:
        raise _http_error(exc) from exc


def _status_fields(status_: object) -> dict[str, object]:
    fields = asdict(status_)  # type: ignore[call-overload]
    fields["embedding"] = fields.pop("identity")
    return dict(fields)
