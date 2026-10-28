import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.exc import IntegrityError

from app.domain.embedding_math import content_hash, validate_result
from app.embeddings.contracts import (
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingUsage,
    EmbeddingValidationError,
)
from app.repositories.document import DocumentRepository
from app.repositories.embedding import EmbeddingRepository, EmbeddingSpace, NewEmbedding
from app.repositories.project import ProjectRepository
from app.services.document import DocumentNotFoundError
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)

BATCH_SIZE = 64

EmbeddingState = Literal["unavailable", "not_indexed", "not_embedded", "embedded", "stale"]
EmbedAction = Literal["embedded", "re-embedded", "unchanged"]


class EmbeddingUnavailableError(Exception):
    def __init__(self) -> None:
        super().__init__("No embedding provider is configured")


class DocumentNotIndexedError(Exception):
    def __init__(self) -> None:
        super().__init__("Index the document before embedding it")


class EmbeddingConflictError(Exception):
    def __init__(self) -> None:
        super().__init__("The document's index changed while embedding; try again")


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    model: str
    dimension: int
    embedder_version: str
    is_test_provider: bool


@dataclass(frozen=True)
class EmbeddingStatus:
    document_id: uuid.UUID
    state: EmbeddingState
    chunk_count: int
    embedded_count: int
    stale_count: int
    identity: ProviderIdentity | None


@dataclass(frozen=True)
class EmbedResult:
    action: EmbedAction
    status: EmbeddingStatus
    usage: EmbeddingUsage | None


def space_of(provider: EmbeddingProvider) -> EmbeddingSpace:
    return EmbeddingSpace(
        provider.provider_name, provider.model_name, provider.dimension, provider.embedder_version
    )


def identity_of(provider: EmbeddingProvider) -> ProviderIdentity:
    return ProviderIdentity(
        provider.provider_name,
        provider.model_name,
        provider.dimension,
        provider.embedder_version,
        provider.is_test_provider,
    )


class EmbeddingService:
    def __init__(
        self,
        embeddings: EmbeddingRepository,
        documents: DocumentRepository,
        projects: ProjectRepository,
        provider: EmbeddingProvider | None,
    ) -> None:
        self._embeddings = embeddings
        self._documents = documents
        self._projects = projects
        self._provider = provider

    def identity(self) -> ProviderIdentity | None:
        return identity_of(self._provider) if self._provider else None

    def _require_document(self, project_id: uuid.UUID, document_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        if self._documents.get(project_id, document_id) is None:
            raise DocumentNotFoundError(document_id)

    def status(self, project_id: uuid.UUID, document_id: uuid.UUID) -> EmbeddingStatus:
        self._require_document(project_id, document_id)
        return self._status(project_id, document_id)

    def _status(self, project_id: uuid.UUID, document_id: uuid.UUID) -> EmbeddingStatus:
        chunks = self._embeddings.document_chunks(project_id, document_id)
        if self._provider is None:
            return EmbeddingStatus(document_id, "unavailable", len(chunks), 0, 0, None)
        identity = identity_of(self._provider)
        if not chunks:
            return EmbeddingStatus(document_id, "not_indexed", 0, 0, 0, identity)
        stored = self._embeddings.stored_hashes(project_id, document_id, space_of(self._provider))
        fresh = sum(1 for c in chunks if stored.get(c.chunk_id) == content_hash(c.text))
        stale = len(stored) - fresh
        if fresh == len(chunks):
            state: EmbeddingState = "embedded"
        elif not stored:
            state = "not_embedded"
        else:
            state = "stale"
        return EmbeddingStatus(document_id, state, len(chunks), fresh, stale, identity)

    def embed(
        self, project_id: uuid.UUID, document_id: uuid.UUID, *, reembed: bool = False
    ) -> EmbedResult:
        self._require_document(project_id, document_id)
        provider = self._provider
        if provider is None:
            raise EmbeddingUnavailableError()
        before = self._status(project_id, document_id)
        if before.state == "not_indexed":
            raise DocumentNotIndexedError()
        if before.state == "embedded" and not reembed:
            return EmbedResult("unchanged", before, None)

        # All provider calls and validation finish before the database is touched, so any
        # failure leaves the previous vectors intact.
        chunks = self._embeddings.document_chunks(project_id, document_id)
        vectors: list[list[float]] = []
        actual_models: list[str | None] = []
        input_tokens: int | None = None
        for start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[start : start + BATCH_SIZE]
            try:
                result = provider.embed([c.text for c in batch])
                vectors.extend(validate_result(result, len(batch), provider.dimension))
            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingProviderError("The embedding provider failed") from exc
            actual_models.extend([result.model] * len(batch))
            if result.usage and result.usage.input_tokens is not None:
                input_tokens = (input_tokens or 0) + result.usage.input_tokens
        items = [
            NewEmbedding(c.chunk_id, content_hash(c.text), vector, model)
            for c, vector, model in zip(chunks, vectors, actual_models, strict=True)
        ]
        try:
            self._embeddings.replace(project_id, document_id, space_of(provider), items)
        except IntegrityError as exc:
            raise EmbeddingConflictError() from exc
        action: EmbedAction = "embedded" if before.state == "not_embedded" else "re-embedded"
        # Identifiers and counts only; never text or vectors.
        logger.info(
            "document %s id=%s project=%s chunks=%d", action, document_id, project_id, len(items)
        )
        usage = EmbeddingUsage(input_tokens=input_tokens) if input_tokens is not None else None
        return EmbedResult(action, self._status(project_id, document_id), usage)


__all__ = [
    "DocumentNotIndexedError",
    "EmbedResult",
    "EmbeddingConflictError",
    "EmbeddingProviderError",
    "EmbeddingService",
    "EmbeddingStatus",
    "EmbeddingUnavailableError",
    "EmbeddingValidationError",
    "ProviderIdentity",
    "identity_of",
    "space_of",
]
