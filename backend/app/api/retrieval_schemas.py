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


class SearchResponse(BaseModel):
    query_terms: list[str]
    results: list[SearchResultResponse]
    scanned_chunks: int
    unindexed_documents: int
    scan_limited: bool
