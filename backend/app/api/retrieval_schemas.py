import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.search import MAX_QUERY_CHARS, MAX_TOP_K


class IndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # False is a no-op for an up-to-date index; True rebuilds it atomically.
    reindex: bool = False


class IndexStatusResponse(BaseModel):
    document_id: uuid.UUID
    state: Literal["not_indexed", "indexed", "stale"]
    chunk_count: int
    chunker_version: str | None
    current_chunker_version: str
    indexed_at: datetime | None


class IndexResponse(IndexStatusResponse):
    action: Literal["indexed", "reindexed", "unchanged"]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS)
    ]
    document_id: uuid.UUID | None = None
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)
    mode: Literal["keyword", "vector", "hybrid"] = "keyword"


class SearchResultResponse(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    source_start: int
    source_end: int
    score: float
    matched_terms: list[str]
    bm25_rank: int | None = None
    bm25_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None


class ProviderIdentityResponse(BaseModel):
    provider: str
    model: str
    dimension: int
    embedder_version: str
    is_test_provider: bool


class SearchResponse(BaseModel):
    query_terms: list[str]
    results: list[SearchResultResponse]
    scanned_chunks: int
    unindexed_documents: int
    scan_limited: bool
    requested_mode: Literal["keyword", "vector", "hybrid"] = "keyword"
    effective_mode: Literal["keyword", "vector", "hybrid", "none"] = "keyword"
    warnings: list[str] = Field(default_factory=list)
    vector_scanned: int = 0
    embedding: ProviderIdentityResponse | None = None


class CapabilitiesResponse(BaseModel):
    modes: list[Literal["keyword", "vector", "hybrid"]]
    vector_available: bool
    vector_unavailable_reason: str | None
    embedding: ProviderIdentityResponse | None


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # False is a no-op for fresh embeddings; True rebuilds them atomically.
    reembed: bool = False


class EmbeddingStatusResponse(BaseModel):
    document_id: uuid.UUID
    state: Literal["unavailable", "not_indexed", "not_embedded", "embedded", "stale"]
    chunk_count: int
    embedded_count: int
    stale_count: int
    embedding: ProviderIdentityResponse | None


class EmbedResponse(EmbeddingStatusResponse):
    action: Literal["embedded", "re-embedded", "unchanged"]
    input_tokens: int | None = None
