import uuid
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_embedding_provider, get_retrieval_service
from app.domain.chunking import ChunkSettings
from app.embeddings.contracts import EmbeddingProviderError, EmbeddingResult
from app.embeddings.fake import FakeEmbeddingProvider
from app.main import app
from app.models import ChunkEmbedding, DocumentChunk
from app.repositories.chunk import ChunkRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.services.retrieval import RetrievalService

SETTINGS = ChunkSettings(size=200, overlap=40)

DOCS = {
    "alpha.txt": "Alpha overview.\n\nThe zephyrquartz module handles billing exports.",
    "beta.txt": "Beta notes.\n\nThe obsidianflux scheduler retries nightly jobs.",
    "gamma.txt": "Gamma notes.\n\nThe lumenvale cache stores entries.",
}


class FlakyProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False
        self.bad_shape = False

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        if self.fail:
            raise EmbeddingProviderError("boom")
        result = super().embed(texts)
        if self.bad_shape:
            result.vectors[0][0] = float("nan")
        return result


class OtherVersionProvider(FakeEmbeddingProvider):
    embedder_version = "fake-hash-v2"


def _project(client: TestClient, name: str = "P") -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return str(response.json()["id"])


def _upload(client: TestClient, pid: str, name: str, content: str) -> str:
    response = client.post(
        f"/api/projects/{pid}/documents", files={"file": (name, content.encode(), "text/plain")}
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _index(client: TestClient, pid: str, did: str) -> None:
    assert client.post(f"/api/projects/{pid}/documents/{did}/index", json={}).status_code == 200


def _embed(client: TestClient, pid: str, did: str, reembed: bool = False) -> dict:  # type: ignore[type-arg]
    response = client.post(f"/api/projects/{pid}/documents/{did}/embed", json={"reembed": reembed})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def _status(client: TestClient, pid: str, did: str) -> dict:  # type: ignore[type-arg]
    response = client.get(f"/api/projects/{pid}/documents/{did}/embedding-status")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def _search(client: TestClient, pid: str, **body: object) -> dict:  # type: ignore[type-arg]
    response = client.post(f"/api/projects/{pid}/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def _count(engine: Engine, document_id: str | None = None) -> int:
    with Session(engine) as session:
        stmt = select(func.count()).select_from(ChunkEmbedding)
        if document_id:
            stmt = stmt.join(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id).where(
                DocumentChunk.document_id == uuid.UUID(document_id)
            )
        return int(session.scalar(stmt) or 0)


@pytest.fixture
def provider() -> FlakyProvider:
    return FlakyProvider()


@pytest.fixture
def small_chunks(client: TestClient, engine: Engine, provider: FlakyProvider) -> Iterator[None]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override() -> Iterator[RetrievalService]:
        with factory() as session:
            yield RetrievalService(
                ChunkRepository(session),
                DocumentRepository(session),
                ProjectRepository(session),
                SETTINGS,
                20000,
            )

    app.dependency_overrides[get_retrieval_service] = override
    app.dependency_overrides[get_embedding_provider] = lambda: provider
    yield
    app.dependency_overrides.pop(get_retrieval_service, None)
    app.dependency_overrides.pop(get_embedding_provider, None)


@pytest.fixture
def atlas(client: TestClient, small_chunks: None) -> tuple[str, dict[str, str]]:
    pid = _project(client)
    ids = {name: _upload(client, pid, name, body) for name, body in DOCS.items()}
    for did in ids.values():
        _index(client, pid, did)
    return pid, ids


def test_default_config_has_no_provider_and_no_outbound_vector_path(client: TestClient) -> None:
    app.dependency_overrides.pop(get_embedding_provider, None)
    pid = _project(client)
    caps = client.get(f"/api/projects/{pid}/retrieval/capabilities").json()
    assert caps["modes"] == ["keyword"]
    assert caps["vector_available"] is False
    assert caps["embedding"] is None


def test_capabilities_with_fake_provider_are_labelled_test(
    client: TestClient, small_chunks: None
) -> None:
    pid = _project(client)
    caps = client.get(f"/api/projects/{pid}/retrieval/capabilities").json()
    assert caps["modes"] == ["keyword", "vector", "hybrid"]
    assert caps["embedding"]["is_test_provider"] is True
    assert caps["embedding"]["provider"] == "fake"
    assert caps["embedding"]["dimension"] == 64


def test_embedding_requires_index_and_reports_status(
    client: TestClient, small_chunks: None
) -> None:
    pid = _project(client)
    did = _upload(client, pid, "a.txt", "hello world")
    assert _status(client, pid, did)["state"] == "not_indexed"
    response = client.post(f"/api/projects/{pid}/documents/{did}/embed", json={})
    assert response.status_code == 409
    _index(client, pid, did)
    assert _status(client, pid, did)["state"] == "not_embedded"
    assert _embed(client, pid, did)["action"] == "embedded"
    status = _status(client, pid, did)
    assert status["state"] == "embedded"
    assert status["embedded_count"] == status["chunk_count"] > 0


def test_embed_without_provider_is_503_and_status_unavailable(client: TestClient) -> None:
    app.dependency_overrides.pop(get_embedding_provider, None)
    pid = _project(client)
    did = _upload(client, pid, "a.txt", "hello")
    assert _status(client, pid, did)["state"] == "unavailable"
    assert client.post(f"/api/projects/{pid}/documents/{did}/embed", json={}).status_code == 503


def test_embedding_is_idempotent_and_explicit_reembed_rebuilds(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    assert _embed(client, pid, did)["action"] == "embedded"
    count = _count(engine)
    assert _embed(client, pid, did)["action"] == "unchanged"
    assert _count(engine) == count
    assert _embed(client, pid, did, reembed=True)["action"] == "re-embedded"
    assert _count(engine) == count


def test_no_automatic_embedding_on_upload_or_index(
    engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    assert _count(engine) == 0


def test_failed_reembed_preserves_previous_vectors(
    client: TestClient,
    engine: Engine,
    atlas: tuple[str, dict[str, str]],
    provider: FlakyProvider,
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    _embed(client, pid, did)
    with Session(engine) as session:
        before = sorted(
            (str(r.id), r.content_hash) for r in session.scalars(select(ChunkEmbedding)).all()
        )
    provider.fail = True
    response = client.post(f"/api/projects/{pid}/documents/{did}/embed", json={"reembed": True})
    assert response.status_code == 502
    assert "boom" not in response.text
    provider.fail = False
    provider.bad_shape = True
    response = client.post(f"/api/projects/{pid}/documents/{did}/embed", json={"reembed": True})
    assert response.status_code == 502
    with Session(engine) as session:
        after = sorted(
            (str(r.id), r.content_hash) for r in session.scalars(select(ChunkEmbedding)).all()
        )
    assert before == after
    assert _status(client, pid, did)["state"] == "embedded"


def test_failed_first_embed_stores_nothing(
    client: TestClient,
    engine: Engine,
    atlas: tuple[str, dict[str, str]],
    provider: FlakyProvider,
) -> None:
    pid, ids = atlas
    provider.fail = True
    assert (
        client.post(f"/api/projects/{pid}/documents/{ids['beta.txt']}/embed", json={}).status_code
        == 502
    )
    assert _count(engine) == 0


def test_stale_content_hash_is_reported_and_skipped(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    _embed(client, pid, did)
    with Session(engine) as session:
        session.execute(update(ChunkEmbedding).values(content_hash="0" * 64))
        session.commit()
    assert _status(client, pid, did)["state"] == "stale"
    result = _search(client, pid, query="zephyrquartz", mode="vector")
    assert result["results"] == []
    assert any("stale" in w for w in result["warnings"])
    assert _embed(client, pid, did)["action"] == "re-embedded"
    assert _status(client, pid, did)["state"] == "embedded"


def test_different_embedder_version_is_a_separate_space(
    client: TestClient, atlas: tuple[str, dict[str, str]], engine: Engine
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    _embed(client, pid, did)
    app.dependency_overrides[get_embedding_provider] = lambda: OtherVersionProvider()
    assert _status(client, pid, did)["state"] == "not_embedded"
    result = _search(client, pid, query="zephyrquartz", mode="vector")
    assert result["effective_mode"] == "none"
    assert _embed(client, pid, did)["action"] == "embedded"
    assert _count(engine, did) == 2 * _status(client, pid, did)["chunk_count"]
    assert _search(client, pid, query="zephyrquartz", mode="vector")["effective_mode"] == "vector"


def test_vector_search_finds_unique_fact_with_source_offsets(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _embed(client, pid, did)
    result = _search(client, pid, query="zephyrquartz billing", mode="vector")
    assert result["requested_mode"] == result["effective_mode"] == "vector"
    top = result["results"][0]
    assert top["filename"] == "alpha.txt"
    assert top["document_id"] == ids["alpha.txt"]
    assert top["vector_rank"] == 1 and top["bm25_rank"] is None
    assert DOCS["alpha.txt"][top["source_start"] : top["source_end"]] == top["text"]
    assert result["embedding"]["is_test_provider"] is True


def test_hybrid_fuses_ranks_and_deduplicates(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _embed(client, pid, did)
    result = _search(client, pid, query="obsidianflux scheduler", mode="hybrid", top_k=10)
    assert result["effective_mode"] == "hybrid"
    chunk_ids = [r["chunk_id"] for r in result["results"]]
    assert len(chunk_ids) == len(set(chunk_ids))
    top = result["results"][0]
    assert top["filename"] == "beta.txt"
    assert top["bm25_rank"] == 1 and top["vector_rank"] == 1
    assert top["score"] == pytest.approx(2 / 61, abs=1e-6)
    again = _search(client, pid, query="obsidianflux scheduler", mode="hybrid", top_k=10)
    assert again == result


def test_hybrid_without_embeddings_is_labelled_keyword_with_warning(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, _ = atlas
    result = _search(client, pid, query="zephyrquartz", mode="hybrid")
    assert result["requested_mode"] == "hybrid"
    assert result["effective_mode"] == "keyword"
    assert any("keyword results only" in w for w in result["warnings"])
    assert result["results"][0]["filename"] == "alpha.txt"


def test_vector_without_embeddings_returns_nothing_with_warning(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, _ = atlas
    result = _search(client, pid, query="zephyrquartz", mode="vector")
    assert result["effective_mode"] == "none"
    assert result["results"] == []
    assert result["warnings"]


def test_hybrid_without_provider_degrades_to_keyword(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, _ = atlas
    app.dependency_overrides[get_embedding_provider] = lambda: None
    hybrid = _search(client, pid, query="zephyrquartz", mode="hybrid")
    assert hybrid["effective_mode"] == "keyword"
    assert any("no embedding provider" in w for w in hybrid["warnings"])
    assert _search(client, pid, query="zephyrquartz", mode="vector")["effective_mode"] == "none"


def test_query_embedding_failure_degrades_with_generic_warning(
    client: TestClient,
    atlas: tuple[str, dict[str, str]],
    provider: FlakyProvider,
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _embed(client, pid, did)
    provider.fail = True
    result = _search(client, pid, query="zephyrquartz", mode="hybrid")
    assert result["effective_mode"] == "keyword"
    assert "boom" not in str(result["warnings"])


def test_keyword_default_is_unchanged_and_independent_of_provider(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, _ = atlas
    app.dependency_overrides[get_embedding_provider] = lambda: None
    result = _search(client, pid, query="zephyrquartz")
    assert result["requested_mode"] == result["effective_mode"] == "keyword"
    assert result["warnings"] == []
    assert result["results"][0]["bm25_rank"] == 1
    assert result["results"][0]["vector_rank"] is None


def test_invalid_query_and_unknown_mode_rejected(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, _ = atlas
    for mode in ("vector", "hybrid"):
        response = client.post(f"/api/projects/{pid}/search", json={"query": "!!!", "mode": mode})
        assert response.status_code == 422
    response = client.post(f"/api/projects/{pid}/search", json={"query": "x", "mode": "magic"})
    assert response.status_code == 422


def test_project_isolation(client: TestClient, atlas: tuple[str, dict[str, str]]) -> None:
    pid, ids = atlas
    for did in ids.values():
        _embed(client, pid, did)
    other = _project(client, "Other")
    foreign = _upload(client, other, "o.txt", "Unrelated zephyrquartz mention.")
    _index(client, other, foreign)
    _embed(client, other, foreign)

    mine = _search(client, pid, query="zephyrquartz", mode="vector")
    assert {r["document_id"] for r in mine["results"]} <= set(ids.values())
    theirs = _search(client, other, query="zephyrquartz", mode="hybrid")
    assert {r["document_id"] for r in theirs["results"]} == {foreign}

    # A document from another project is never reachable.
    assert (
        client.post(
            f"/api/projects/{other}/documents/{ids['alpha.txt']}/embed", json={}
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/projects/{other}/documents/{ids['alpha.txt']}/embedding-status"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/projects/{other}/search",
            json={"query": "zephyrquartz", "mode": "vector", "document_id": ids["alpha.txt"]},
        ).status_code
        == 404
    )


def test_reindex_and_document_delete_remove_embeddings(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _embed(client, pid, did)
    alpha, beta = ids["alpha.txt"], ids["beta.txt"]
    assert _count(engine, alpha) > 0
    assert (
        client.post(
            f"/api/projects/{pid}/documents/{alpha}/index", json={"reindex": True}
        ).status_code
        == 200
    )
    assert _count(engine, alpha) == 0
    assert _status(client, pid, alpha)["state"] == "not_embedded"
    assert _count(engine, beta) > 0

    total = _count(engine)
    assert client.delete(f"/api/projects/{pid}/documents/{beta}").status_code == 204
    assert _count(engine) < total
    with Session(engine) as session:
        orphans = session.scalar(
            select(func.count())
            .select_from(ChunkEmbedding)
            .outerjoin(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
            .where(DocumentChunk.id.is_(None))
        )
    assert orphans == 0


def test_project_delete_removes_embeddings(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _embed(client, pid, did)
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert _count(engine) == 0


def test_embeddings_persist_across_sessions(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    _embed(client, pid, ids["alpha.txt"])
    with Session(engine) as session:
        row = session.scalars(select(ChunkEmbedding)).first()
        assert row is not None
        assert row.provider == "fake" and row.dimension == 64
        assert row.embedder_version == "fake-hash-v1"
        assert row.actual_model == "fake-hash-embedding"
        assert len(row.vector) == 64 and all(isinstance(x, float) for x in row.vector)
        assert row.created_at is not None
