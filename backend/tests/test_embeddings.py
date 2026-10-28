import math

import pytest

from app.domain.embedding_math import (
    content_hash,
    cosine,
    reciprocal_rank_fusion,
    validate_result,
    validate_vector,
)
from app.embeddings.contracts import EmbeddingResult, EmbeddingValidationError
from app.embeddings.factory import create_embedding_provider
from app.embeddings.fake import FakeEmbeddingProvider


def test_fake_provider_is_deterministic_and_labelled_as_test() -> None:
    provider = FakeEmbeddingProvider()
    first = provider.embed(["alpha beta", "gamma"])
    second = FakeEmbeddingProvider().embed(["alpha beta", "gamma"])
    assert first.vectors == second.vectors
    assert first.dimension == provider.dimension == len(first.vectors[0])
    assert first.model == "fake-hash-embedding"
    assert provider.is_test_provider is True
    assert "TEST PROVIDER ONLY" in (FakeEmbeddingProvider.__doc__ or "")


def test_fake_vectors_are_never_zero_and_capture_overlap_only() -> None:
    provider = FakeEmbeddingProvider()
    punct, same_a, same_b, other = provider.embed(["---", "red car", "car red", "quantum"]).vectors
    assert any(punct)
    assert cosine(same_a, same_b) == pytest.approx(1.0)
    assert cosine(same_a, other) < 0.5


def test_factory_returns_none_without_provider_setting() -> None:
    class S:
        embedding_provider = "none"

    assert create_embedding_provider(S()) is None  # type: ignore[arg-type]
    S.embedding_provider = "fake"
    assert isinstance(create_embedding_provider(S()), FakeEmbeddingProvider)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "vector",
    [
        [1.0, float("nan"), 0.0],
        [1.0, float("inf"), 0.0],
        [1.0, True, 0.0],
        [1.0, "2", 0.0],
        [1.0, 2.0],
        [0.0, 0.0, 0.0],
        "abc",
        None,
    ],
)
def test_malformed_vectors_are_rejected(vector: object) -> None:
    with pytest.raises(EmbeddingValidationError):
        validate_vector(vector, 3)


def test_valid_vector_is_returned_as_floats() -> None:
    assert validate_vector([1, 2, 3], 3) == [1.0, 2.0, 3.0]


def test_result_count_and_dimension_mismatch_are_rejected() -> None:
    result = EmbeddingResult(vectors=[[1.0, 0.0]], dimension=2)
    with pytest.raises(EmbeddingValidationError):
        validate_result(result, 2, 2)
    with pytest.raises(EmbeddingValidationError):
        validate_result(result, 1, 3)
    with pytest.raises(EmbeddingValidationError):
        validate_result(EmbeddingResult(vectors=[[1.0, 0.0]], dimension=3), 1, 2)
    assert validate_result(result, 1, 2) == [[1.0, 0.0]]


def test_cosine_basics() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([1.0, 1.0], [2.0, 2.0]) == pytest.approx(1.0)
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert math.isfinite(cosine([1e200, 1e200], [1e200, 1e200]))


def test_rrf_combines_ranks_and_deduplicates() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "a", "d"]], k=60)
    assert set(fused) == {"a", "b", "c", "d"}
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert fused["a"] > fused["c"] > fused["b"] > fused["d"]
    assert fused["b"] == pytest.approx(1 / 62)


def test_rrf_is_deterministic() -> None:
    rankings = [["x", "y"], ["y", "x"]]
    assert reciprocal_rank_fusion(rankings) == reciprocal_rank_fusion(rankings)


def test_content_hash_is_sha256_of_utf8() -> None:
    assert len(content_hash("caf\u00e9")) == 64
    assert content_hash("a") != content_hash("b")
