import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.domain.embedding_math import content_hash, cosine, reciprocal_rank_fusion
from app.domain.search import MAX_TOP_K, parse_query
from app.embeddings.contracts import EmbeddingProvider
from app.repositories.embedding import EmbeddingRepository, VectorRow
from app.services.embedding import ProviderIdentity, identity_of, space_of
from app.services.retrieval import RetrievalService, SearchHit

logger = logging.getLogger(__name__)

SearchMode = Literal["keyword", "vector", "hybrid"]
EffectiveMode = Literal["keyword", "vector", "hybrid", "none"]


@dataclass(frozen=True)
class RankedHit:
    hit: SearchHit
    score: float
    bm25_rank: int | None
    bm25_score: float | None
    vector_rank: int | None
    vector_score: float | None


@dataclass(frozen=True)
class HybridOutcome:
    requested_mode: SearchMode
    effective_mode: EffectiveMode
    terms: list[str]
    hits: list[RankedHit]
    scanned_chunks: int
    unindexed_documents: int
    scan_limited: bool
    vector_scanned: int
    warnings: list[str] = field(default_factory=list)
    identity: ProviderIdentity | None = None


@dataclass(frozen=True)
class _VectorHit:
    row: VectorRow
    score: float


class HybridRetrievalService:
    """Keyword, vector or rank-fused search. Keyword search stays independently usable.

    The effective mode reports what actually produced the results; keyword-only results are
    never presented as hybrid.
    """

    def __init__(
        self,
        retrieval: RetrievalService,
        embeddings: EmbeddingRepository,
        provider: EmbeddingProvider | None,
        scan_limit: int,
    ) -> None:
        self._retrieval = retrieval
        self._embeddings = embeddings
        self._provider = provider
        self._scan_limit = scan_limit

    def search(
        self,
        project_id: uuid.UUID,
        query: str,
        *,
        mode: SearchMode = "keyword",
        document_id: uuid.UUID | None = None,
        top_k: int = 5,
    ) -> HybridOutcome:
        top_k = max(1, min(top_k, MAX_TOP_K))
        if mode == "keyword":
            outcome = self._retrieval.search(
                project_id, query, document_id=document_id, top_k=top_k
            )
            return HybridOutcome(
                "keyword",
                "keyword",
                outcome.terms,
                [
                    RankedHit(h, h.score, i, h.score, None, None)
                    for i, h in enumerate(outcome.hits, 1)
                ],
                outcome.scanned_chunks,
                outcome.unindexed_documents,
                outcome.scan_limited,
                0,
                [],
                identity_of(self._provider) if self._provider else None,
            )

        self._retrieval.check_scope(project_id, document_id)
        terms = parse_query(query)
        identity = identity_of(self._provider) if self._provider else None
        warnings: list[str] = []
        vector_hits, vector_scanned, vector_limited = self._vector_path(
            project_id, query, document_id, warnings
        )
        keyword = self._retrieval.search(
            project_id, query, document_id=document_id, top_k=MAX_TOP_K
        )
        unindexed = keyword.unindexed_documents
        limited = keyword.scan_limited or vector_limited

        if vector_hits is None:
            if mode == "vector":
                return HybridOutcome(
                    mode, "none", terms, [], 0, unindexed, limited, 0, warnings, identity
                )
            warnings.append("Hybrid search is unavailable; showing keyword results only.")
            hits = [
                RankedHit(h, h.score, i, h.score, None, None)
                for i, h in enumerate(keyword.hits[:top_k], 1)
            ]
            return HybridOutcome(
                mode,
                "keyword",
                terms,
                hits,
                keyword.scanned_chunks,
                unindexed,
                limited,
                0,
                warnings,
                identity,
            )

        if mode == "vector":
            hits = [
                RankedHit(self._to_hit(v.row, v.score), v.score, None, None, i, v.score)
                for i, v in enumerate(vector_hits[:top_k], 1)
            ]
            return HybridOutcome(
                mode,
                "vector",
                terms,
                hits,
                0,
                unindexed,
                limited,
                vector_scanned,
                warnings,
                identity,
            )

        return HybridOutcome(
            mode,
            "hybrid",
            terms,
            self._fuse(keyword.hits, vector_hits, top_k),
            keyword.scanned_chunks,
            unindexed,
            limited,
            vector_scanned,
            warnings,
            identity,
        )

    def _to_hit(self, row: VectorRow, score: float) -> SearchHit:
        return SearchHit(
            row.chunk_id,
            row.document_id,
            row.filename,
            row.chunk_index,
            row.text,
            row.source_start,
            row.source_end,
            score,
            (),
        )

    def _vector_path(
        self,
        project_id: uuid.UUID,
        query: str,
        document_id: uuid.UUID | None,
        warnings: list[str],
    ) -> tuple[list[_VectorHit] | None, int, bool]:
        provider = self._provider
        if provider is None:
            warnings.append("Vector search is unavailable: no embedding provider is configured.")
            return None, 0, False
        space = space_of(provider)
        rows = self._embeddings.scan(project_id, document_id, space, self._scan_limit + 1)
        limited = len(rows) > self._scan_limit
        rows = rows[: self._scan_limit]
        if not rows:
            warnings.append("Vector search is unavailable: no embeddings exist for this scope.")
            return None, 0, False
        try:
            result = provider.embed([query])
            query_vector = result.vectors[0]
            if len(result.vectors) != 1 or len(query_vector) != provider.dimension:
                raise ValueError("bad shape")
        except Exception:
            warnings.append("Vector search is unavailable: the query could not be embedded.")
            return None, 0, False

        usable: list[_VectorHit] = []
        stale = 0
        for row in rows:
            if row.content_hash != content_hash(row.text) or len(row.vector) != provider.dimension:
                stale += 1
                continue
            try:
                score = round(cosine(query_vector, [float(x) for x in row.vector]), 6)
            except (TypeError, ValueError):
                stale += 1
                continue
            if score > 0:
                usable.append(_VectorHit(row, score))
        if stale:
            warnings.append(f"{stale} stale or invalid embeddings were skipped; re-embed them.")
        indexed = self._embeddings.chunk_count(project_id, document_id)
        if indexed > len(rows):
            warnings.append(
                f"Only {len(rows)} of {indexed} indexed chunks have embeddings in this scope."
            )
        if limited:
            warnings.append("Vector scan limit reached; results may be incomplete.")
        usable.sort(
            key=lambda v: (-v.score, v.row.filename, str(v.row.document_id), v.row.chunk_index)
        )
        return usable[:MAX_TOP_K], len(rows), limited

    def _fuse(
        self, keyword: list[SearchHit], vector: list[_VectorHit], top_k: int
    ) -> list[RankedHit]:
        by_id: dict[str, SearchHit] = {}
        bm25: dict[str, tuple[int, float]] = {}
        vec: dict[str, tuple[int, float]] = {}
        for rank, hit in enumerate(keyword, 1):
            by_id[str(hit.chunk_id)] = hit
            bm25[str(hit.chunk_id)] = (rank, hit.score)
        for rank, v in enumerate(vector, 1):
            key = str(v.row.chunk_id)
            by_id.setdefault(key, self._to_hit(v.row, v.score))
            vec[key] = (rank, v.score)
        fused = reciprocal_rank_fusion([list(bm25), list(vec)])
        ordered = sorted(
            fused,
            key=lambda k: (
                -fused[k],
                by_id[k].filename,
                str(by_id[k].document_id),
                by_id[k].chunk_index,
            ),
        )
        return [
            RankedHit(
                by_id[k],
                round(fused[k], 6),
                bm25[k][0] if k in bm25 else None,
                bm25[k][1] if k in bm25 else None,
                vec[k][0] if k in vec else None,
                vec[k][1] if k in vec else None,
            )
            for k in ordered[:top_k]
        ]
