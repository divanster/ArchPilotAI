"""Deterministic lexical ranking (BM25) over document chunks. No embeddings, no LLM.

Scoring: Okapi BM25 with k1=1.2 and b=0.75 over the chunks in scope (the project, or one document
of it). For each distinct query term t and chunk d:

    idf(t)   = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
    score(d) = sum over t of idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg_len))

N is the number of chunks scanned, df the number of those containing t, tf the occurrences of t in
d and len(d) its token count. Scores are rounded to 6 decimals; ties break on filename, document id
and chunk index, so identical data always yields identical order.

Limitations (baseline, by design):
- Exact token matching after NFKC normalization and case folding: no stemming, synonyms, typo
  tolerance, phrase or proximity scoring, and no semantic similarity.
- Tokens are runs of Unicode word characters; languages written without spaces (e.g. Chinese)
  match only whole runs.
- Statistics are computed on the chunks scanned for each request, so scores are comparable within
  one response only, and idf shifts as documents are added or filtered.
- All in-scope chunks are scanned in Python (bounded by a scan limit) rather than via a database
  index, which keeps behaviour identical on PostgreSQL and SQLite.
"""

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 20
MAX_TOP_K = 20
K1 = 1.2
B = 0.75

_TOKEN = re.compile(r"\w+")


class InvalidQueryError(ValueError):
    pass


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


def parse_query(query: str) -> list[str]:
    """Distinct query terms in first-seen order."""
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidQueryError(f"Query must be at most {MAX_QUERY_CHARS} characters")
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        raise InvalidQueryError("Query must contain at least one letter or digit")
    if len(terms) > MAX_QUERY_TERMS:
        raise InvalidQueryError(f"Query must have at most {MAX_QUERY_TERMS} distinct terms")
    return terms


@dataclass(frozen=True)
class Scorable:
    chunk_id: str
    filename: str
    document_id: str
    chunk_index: int
    text: str


@dataclass(frozen=True)
class Scored:
    item: Scorable
    score: float
    matched_terms: tuple[str, ...]


def rank(items: Sequence[Scorable], terms: Sequence[str], top_k: int) -> list[Scored]:
    if not items or not terms:
        return []
    counts = [Counter(tokenize(item.text)) for item in items]
    lengths = [sum(c.values()) for c in counts]
    n = len(items)
    average = (sum(lengths) / n) or 1.0
    document_frequency = {t: sum(1 for c in counts if c[t]) for t in terms}

    scored: list[Scored] = []
    for item, tokens, length in zip(items, counts, lengths, strict=True):
        score = 0.0
        matched: list[str] = []
        for term in terms:
            tf = tokens[term]
            if not tf:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / average))
            matched.append(term)
        if matched:
            scored.append(Scored(item, round(score, 6), tuple(matched)))
    scored.sort(
        key=lambda s: (-s.score, s.item.filename, s.item.document_id, s.item.chunk_index),
    )
    return scored[:top_k]
