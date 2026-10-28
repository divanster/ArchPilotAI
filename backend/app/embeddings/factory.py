from app.core.config import Settings
from app.embeddings.contracts import EmbeddingProvider
from app.embeddings.fake import FakeEmbeddingProvider


def create_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """None means vector search is unavailable; keyword search is unaffected.

    A Workbench provider is intentionally absent until its embedding API contract is confirmed.
    """
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider()
    return None
