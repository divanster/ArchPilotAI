"""Retrieval of unconfirmed document passages for architecture generation (keyword search only).

Queries are built from the confirmed requirements, in alias order, with no randomness and no
model call. Each requirement ranks the project's indexed chunks with the same BM25 baseline as
the search feature; the picks are merged round-robin (every requirement's best hit first), deduped
by chunk, and capped by a character budget. Nothing is indexed here: unindexed documents are
reported, not processed. Limitations: exact-token matching only (no synonyms), and the first
20 distinct words of each requirement form its query.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.architecture import RetrievalInfo
from app.domain.search import MAX_QUERY_TERMS, Scorable, rank, tokenize
from app.repositories.chunk import ChunkRepository, ChunkRow

MIN_TERM_CHARS = 3
FIRST_ALIAS = "P001"  # every alias has the same width, so a placeholder sizes an entry exactly


@dataclass(frozen=True)
class RequirementQuery:
    alias: str
    title: str
    description: str


@dataclass(frozen=True)
class RetrievedContext:
    passages: list[ChunkRow]  # in alias order: P001 is passages[0]
    info: RetrievalInfo


def query_terms(title: str, description: str) -> list[str]:
    """Distinct words (3+ characters) of the requirement text, first-seen order, at most 20."""
    words = (t for t in tokenize(f"{title} {description}") if len(t) >= MIN_TERM_CHARS)
    return list(dict.fromkeys(words))[:MAX_QUERY_TERMS]


def passage_cost(filename: str, text: str) -> int:
    """Characters one passage adds to the JSON context (wrapper and separator included)."""
    entry = {"id": FIRST_ALIAS, "status": "unconfirmed", "document": filename, "excerpt": text}
    return len(json.dumps(entry, ensure_ascii=False)) + 2


class ArchitectureContextRetriever:
    def __init__(
        self,
        chunks: ChunkRepository | None,
        *,
        scan_limit: int,
        per_requirement: int,
        max_passages: int,
    ) -> None:
        self._chunks = chunks
        self._scan_limit = scan_limit
        self._per_requirement = per_requirement
        self._max_passages = max_passages

    def retrieve(
        self,
        project_id: uuid.UUID,
        queries: Sequence[RequirementQuery],
        *,
        has_documents: bool,
        budget_chars: int,
    ) -> RetrievedContext:
        def info(
            status: str, count: int = 0, unindexed: int = 0, cut: bool = False
        ) -> RetrievedContext:
            return RetrievedContext(
                [],
                RetrievalInfo.model_validate(
                    {
                        "status": status,
                        "passages_provided": count,
                        "unindexed_documents": unindexed,
                        "budget_chars": max(0, budget_chars),
                        "truncated": cut,
                    }
                ),
            )

        if self._chunks is None or budget_chars <= 0:
            return info("disabled")
        if not has_documents:
            return info("no_documents")
        unindexed = self._chunks.unindexed_document_count(project_id, None)
        rows = self._chunks.scan(project_id, None, self._scan_limit)
        if not rows:
            return info("not_indexed", unindexed=unindexed)

        by_id = {str(r.chunk_id): r for r in rows}
        items = [
            Scorable(str(r.chunk_id), r.filename, str(r.document_id), r.chunk_index, r.text)
            for r in rows
        ]
        ranked = [
            [s.item.chunk_id for s in rank(items, terms, self._per_requirement)]
            for q in queries
            if (terms := query_terms(q.title, q.description))
        ]

        chosen: dict[str, ChunkRow] = {}
        remaining = budget_chars
        truncated = False
        for position in range(self._per_requirement):
            for hits in ranked:
                if position >= len(hits) or hits[position] in chosen:
                    continue
                if len(chosen) >= self._max_passages:
                    truncated = True
                    continue
                row = by_id[hits[position]]
                cost = passage_cost(row.filename, row.text)
                if cost > remaining:
                    truncated = True
                    continue
                chosen[hits[position]] = row
                remaining -= cost

        if not chosen:
            return info("no_matches", unindexed=unindexed, cut=truncated)
        ordered = sorted(
            chosen.values(), key=lambda r: (r.filename, str(r.document_id), r.chunk_index)
        )
        result = info("used", len(ordered), unindexed, truncated)
        return RetrievedContext(ordered, result.info)
