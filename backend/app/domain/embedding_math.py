"""Pure helpers for vectors, cosine similarity and reciprocal rank fusion."""

import hashlib
import math
from collections.abc import Iterable, Sequence

from app.embeddings.contracts import EmbeddingResult, EmbeddingValidationError

RRF_K = 60


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_vector(vector: object, dimension: int) -> list[float]:
    """Return a clean float list or raise; never echoes the offending values."""
    if not isinstance(vector, list | tuple):
        raise EmbeddingValidationError("vector is not a sequence")
    if len(vector) != dimension:
        raise EmbeddingValidationError(f"vector length does not match dimension {dimension}")
    clean: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EmbeddingValidationError("vector contains a non-numeric value")
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingValidationError("vector contains a non-finite value")
        clean.append(number)
    if math.hypot(*clean) == 0.0:
        raise EmbeddingValidationError("vector has a zero norm")
    return clean


def validate_result(
    result: EmbeddingResult, expected_count: int, dimension: int
) -> list[list[float]]:
    if result.dimension != dimension:
        raise EmbeddingValidationError("provider dimension differs from the declared dimension")
    if len(result.vectors) != expected_count:
        raise EmbeddingValidationError("provider returned the wrong number of vectors")
    return [validate_vector(v, dimension) for v in result.vectors]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("dimension mismatch")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.hypot(*a) * math.hypot(*b)
    if norm == 0.0 or not math.isfinite(dot):
        # Rescale to avoid overflow on huge magnitudes.
        scale_a = max(map(abs, a), default=0.0)
        scale_b = max(map(abs, b), default=0.0)
        if scale_a == 0.0 or scale_b == 0.0:
            return 0.0
        return cosine([x / scale_a for x in a], [y / scale_b for y in b])
    return max(-1.0, min(1.0, dot / norm))


def reciprocal_rank_fusion(rankings: Iterable[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Sum ``1 / (k + rank)`` over each ranked list of ids (rank is 1-based)."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for position, key in enumerate(ranking, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + position)
    return fused
