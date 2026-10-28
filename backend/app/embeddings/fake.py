import hashlib
from collections import Counter
from collections.abc import Sequence

from app.domain.search import tokenize
from app.embeddings.contracts import EmbeddingResult

FAKE_DIMENSION = 64


class FakeEmbeddingProvider:
    """TEST PROVIDER ONLY. Not a semantic model.

    Hashes each token into one of ``FAKE_DIMENSION`` buckets, so texts that share words get a
    high cosine similarity and unrelated texts get none. It captures lexical overlap, never
    meaning (no synonyms, paraphrase or cross-language understanding). It is deterministic,
    offline and free, which makes retrieval plumbing testable.
    """

    provider_name = "fake"
    model_name = "fake-hash-embedding"
    embedder_version = "fake-hash-v1"
    is_test_provider = True

    def __init__(self, dimension: int = FAKE_DIMENSION) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        # Text without word characters (e.g. "---") hashes as itself so the vector is never zero.
        tokens = tokenize(text) or [text.strip() or " "]
        vector = [0.0] * self.dimension
        for token, count in Counter(tokens).items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimension] += float(count)
        return vector

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=[self._vector(t) for t in texts],
            dimension=self.dimension,
            model=self.model_name,
            usage=None,
        )
