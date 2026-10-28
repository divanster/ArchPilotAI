"""Provider-independent embedding contract.

Deliberately separate from ``LLMProvider``: embedding endpoints have their own routes, request
shapes and limits. No Workbench embedding provider exists until its API contract is confirmed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class EmbeddingError(Exception):
    """Base class. Messages never contain input text or vector values."""


class EmbeddingProviderError(EmbeddingError):
    """The provider failed or is unreachable."""


class EmbeddingValidationError(EmbeddingError):
    """The provider returned vectors that violate the contract."""


@dataclass(frozen=True)
class EmbeddingUsage:
    input_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    dimension: int
    # The model the provider reports having used, when it reports one.
    model: str | None = None
    usage: EmbeddingUsage | None = None


class EmbeddingProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    # Configured model/deployment identity; with the dimension and ``embedder_version`` it
    # defines the embedding space. Vectors from different spaces are never compared.
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    # Bump when the provider's behaviour changes in a way that invalidates stored vectors.
    @property
    def embedder_version(self) -> str: ...

    @property
    def is_test_provider(self) -> bool: ...

    def embed(self, texts: Sequence[str]) -> EmbeddingResult: ...
