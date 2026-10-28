import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_retrieval_service
from app.domain.chunking import ChunkSettings
from app.domain.search import MAX_QUERY_CHARS, MAX_QUERY_TERMS, parse_query, tokenize
from app.main import app
from app.models import DocumentChunk
from app.repositories.chunk import ChunkRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.services import retrieval as retrieval_module
from app.services.retrieval import RetrievalService

SETTINGS = ChunkSettings(size=200, overlap=40)

DOCS = {
    "alpha.txt": "Alpha overview.\r\n\r\nThe zephyrquartz module handles billing exports.\r\n\r\n"
    "Shared paragraph about reports and dashboards.",
    "beta.txt": "Beta notes.\n\nThe obsidianflux scheduler retries nightly jobs.\n\n"
    "Shared paragraph about reports and dashboards.",
    "gamma.txt": "Gamma caf\u00e9 \u6771\u4eac text.\n\n"
    "The lumenvale cache stores na\u00efve entries.",
}


def _project(client: TestClient, name: str = "P") -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return str(response.json()["id"])


def _upload(client: TestClient, pid: str, name: str, content: str) -> str:
    response = client.post(
        f"/api/projects/{pid}/documents",
        files={"file": (name, content.encode(), "text/plain")},
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _index(client: TestClient, pid: str, did: str, reindex: bool = False) -> dict:  # type: ignore[type-arg]
    response = client.post(f"/api/projects/{pid}/documents/{did}/index", json={"reindex": reindex})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def _search(client: TestClient, pid: str, **body: object) -> dict:  # type: ignore[type-arg]
    response = client.post(f"/api/projects/{pid}/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.fixture
def small_chunks(client: TestClient, engine: Engine) -> Iterator[None]:
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
    yield
    app.dependency_overrides.pop(get_retrieval_service, None)


@pytest.fixture
def atlas(client: TestClient, small_chunks: None) -> tuple[str, dict[str, str]]:
    pid = _project(client)
    ids = {name: _upload(client, pid, name, body) for name, body in DOCS.items()}
    return pid, ids


def _chunk_count(engine: Engine, document_id: str | None = None) -> int:
    with Session(engine) as session:
        stmt = select(func.count()).select_from(DocumentChunk)
        if document_id:
            stmt = stmt.where(DocumentChunk.document_id == uuid.UUID(document_id))
        return int(session.scalar(stmt) or 0)


def test_upload_does_not_auto_index(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    assert _chunk_count(engine) == 0
    status = client.get(f"/api/projects/{pid}/documents/{ids['alpha.txt']}/index-status").json()
    assert status["state"] == "not_indexed"
    assert status["chunk_count"] == 0


def test_index_persists_exact_offsets(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _index(client, pid, did)
    with Session(engine) as session:
        for name, did in ids.items():
            rows = session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == uuid.UUID(did))
                .order_by(DocumentChunk.chunk_index)
            ).all()
            assert rows
            assert [r.chunk_index for r in rows] == list(range(len(rows)))
            for row in rows:
                assert DOCS[name][row.source_start : row.source_end] == row.text
                assert row.project_id == uuid.UUID(pid)
                assert row.chunker_version == SETTINGS.version


def test_index_is_idempotent(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    first = _index(client, pid, did)
    assert first["action"] == "indexed"
    with Session(engine) as session:
        before = {str(c.id) for c in session.scalars(select(DocumentChunk))}
    again = _index(client, pid, did)
    assert again["action"] == "unchanged"
    assert again["chunk_count"] == first["chunk_count"]
    with Session(engine) as session:
        assert {str(c.id) for c in session.scalars(select(DocumentChunk))} == before
    forced = _index(client, pid, did, reindex=True)
    assert forced["action"] == "reindexed"
    assert forced["chunk_count"] == first["chunk_count"]
    assert _chunk_count(engine, did) == first["chunk_count"]


def test_settings_change_marks_index_stale_and_reindex_refreshes(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    _index(client, pid, did)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override() -> Iterator[RetrievalService]:
        with factory() as session:
            yield RetrievalService(
                ChunkRepository(session),
                DocumentRepository(session),
                ProjectRepository(session),
                ChunkSettings(size=120, overlap=0),
                20000,
            )

    app.dependency_overrides[get_retrieval_service] = override
    status = client.get(f"/api/projects/{pid}/documents/{did}/index-status").json()
    assert status["state"] == "stale"
    assert status["chunker_version"] == SETTINGS.version
    # A stale index stays searchable.
    assert _search(client, pid, query="zephyrquartz")["results"]
    refreshed = _index(client, pid, did)
    assert refreshed["action"] == "reindexed"
    assert refreshed["state"] == "indexed"
    assert refreshed["chunker_version"] == "para-v1:120:0"


def test_failed_reindex_preserves_previous_index(
    client: TestClient,
    engine: Engine,
    atlas: tuple[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    _index(client, pid, did)
    with Session(engine) as session:
        before = sorted(
            (c.chunk_index, c.text, c.source_start, c.source_end, str(c.id))
            for c in session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == uuid.UUID(did))
            )
        )
    real = retrieval_module.chunk_text  # type: ignore[attr-defined]

    def duplicate_indexes(document_text: str, settings: ChunkSettings) -> list:  # type: ignore[type-arg]
        chunks = real(document_text, settings)
        return [chunks[0], chunks[0]]

    monkeypatch.setattr(retrieval_module, "chunk_text", duplicate_indexes)
    response = client.post(f"/api/projects/{pid}/documents/{did}/index", json={"reindex": True})
    assert response.status_code == 409
    monkeypatch.setattr(retrieval_module, "chunk_text", real)
    with Session(engine) as session:
        after = sorted(
            (c.chunk_index, c.text, c.source_start, c.source_end, str(c.id))
            for c in session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == uuid.UUID(did))
            )
        )
    assert after == before
    assert _search(client, pid, query="zephyrquartz")["results"]


def test_search_finds_unique_fact_in_correct_document(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _index(client, pid, did)
    for term, name in [
        ("zephyrquartz", "alpha.txt"),
        ("obsidianflux", "beta.txt"),
        ("lumenvale", "gamma.txt"),
    ]:
        results = _search(client, pid, query=term)["results"]
        assert len(results) == 1
        hit = results[0]
        assert hit["filename"] == name
        assert hit["document_id"] == ids[name]
        assert DOCS[name][hit["source_start"] : hit["source_end"]] == hit["text"]
        assert hit["score"] > 0
        assert hit["matched_terms"] == [term]


def test_unicode_case_and_accent_queries(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _index(client, pid, did)
    assert _search(client, pid, query="LUMENVALE")["results"][0]["filename"] == "gamma.txt"
    assert _search(client, pid, query="\u6771\u4eac")["results"][0]["filename"] == "gamma.txt"
    assert _search(client, pid, query="caf\u00e9")["results"][0]["filename"] == "gamma.txt"


def test_document_filter_top_k_and_deterministic_ties(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _index(client, pid, did)
    first = _search(client, pid, query="shared paragraph dashboards", top_k=1)
    assert len(first["results"]) == 1
    both = _search(client, pid, query="shared paragraph dashboards", top_k=5)
    assert len(both["results"]) >= 2
    scores = [r["score"] for r in both["results"]]
    assert scores == sorted(scores, reverse=True)
    # Equal-scoring chunks tie-break by filename, so alpha precedes beta.
    names = [r["filename"] for r in both["results"]]
    assert names.index("alpha.txt") < names.index("beta.txt")
    assert both == _search(client, pid, query="shared paragraph dashboards", top_k=5)
    assert first["results"][0] == both["results"][0]
    filtered = _search(
        client, pid, query="shared paragraph dashboards", document_id=ids["beta.txt"]
    )
    assert {r["filename"] for r in filtered["results"]} == {"beta.txt"}


def test_no_match_and_unindexed_reporting(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    before = _search(client, pid, query="zephyrquartz")
    assert before["results"] == []
    assert before["unindexed_documents"] == 3
    assert before["scanned_chunks"] == 0
    _index(client, pid, ids["alpha.txt"])
    after = _search(client, pid, query="nonexistentword")
    assert after["results"] == []
    assert after["unindexed_documents"] == 2
    assert after["scanned_chunks"] > 0


@pytest.mark.parametrize("query", ["", "   ", "!!! ??? ...", "x" * (MAX_QUERY_CHARS + 1)])
def test_invalid_queries_are_rejected(
    client: TestClient, atlas: tuple[str, dict[str, str]], query: str
) -> None:
    pid, _ = atlas
    response = client.post(f"/api/projects/{pid}/search", json={"query": query})
    assert response.status_code == 422


def test_too_many_terms_and_bad_top_k_rejected(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, _ = atlas
    many = " ".join(f"term{i}" for i in range(MAX_QUERY_TERMS + 1))
    assert client.post(f"/api/projects/{pid}/search", json={"query": many}).status_code == 422
    for top_k in (0, 21):
        r = client.post(f"/api/projects/{pid}/search", json={"query": "abc", "top_k": top_k})
        assert r.status_code == 422


def test_sql_metacharacters_in_query_are_inert(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    _index(client, pid, ids["alpha.txt"])
    result = _search(client, pid, query="'; DROP TABLE document_chunks; -- % _")
    assert result["results"] == []
    assert _chunk_count(engine) > 0


def test_project_isolation(client: TestClient, small_chunks: None) -> None:
    one, two = _project(client, "One"), _project(client, "Two")
    doc_one = _upload(client, one, "one.txt", "The isolatedfact lives only here.")
    doc_two = _upload(client, two, "two.txt", "The isolatedfact also lives there, twice.")
    _index(client, one, doc_one)
    _index(client, two, doc_two)
    hits = _search(client, one, query="isolatedfact")["results"]
    assert [h["document_id"] for h in hits] == [doc_one]
    # Cross-project document ids are 404 for index, status and search filters.
    assert client.post(f"/api/projects/{one}/documents/{doc_two}/index", json={}).status_code == 404
    assert client.get(f"/api/projects/{one}/documents/{doc_two}/index-status").status_code == 404
    assert (
        client.post(
            f"/api/projects/{one}/search", json={"query": "isolatedfact", "document_id": doc_two}
        ).status_code
        == 404
    )
    missing = str(uuid.uuid4())
    assert client.post(f"/api/projects/{missing}/search", json={"query": "abc"}).status_code == 404
    assert client.post(f"/api/projects/{one}/documents/{missing}/index").status_code == 404


def test_index_without_body_defaults_to_no_reindex(
    client: TestClient, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    did = ids["alpha.txt"]
    assert client.post(f"/api/projects/{pid}/documents/{did}/index").json()["action"] == "indexed"
    assert client.post(f"/api/projects/{pid}/documents/{did}/index").json()["action"] == "unchanged"


def test_document_deletion_removes_chunks_only_for_that_document(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _index(client, pid, did)
    other = _chunk_count(engine, ids["beta.txt"])
    assert client.delete(f"/api/projects/{pid}/documents/{ids['alpha.txt']}").status_code == 204
    assert _chunk_count(engine, ids["alpha.txt"]) == 0
    assert _chunk_count(engine, ids["beta.txt"]) == other
    assert _search(client, pid, query="zephyrquartz")["results"] == []


def test_project_deletion_removes_chunks(
    client: TestClient, engine: Engine, atlas: tuple[str, dict[str, str]]
) -> None:
    pid, ids = atlas
    for did in ids.values():
        _index(client, pid, did)
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert _chunk_count(engine) == 0


def test_scan_limit_is_reported(client: TestClient, engine: Engine) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override() -> Iterator[RetrievalService]:
        with factory() as session:
            yield RetrievalService(
                ChunkRepository(session),
                DocumentRepository(session),
                ProjectRepository(session),
                ChunkSettings(size=100, overlap=0),
                2,
            )

    app.dependency_overrides[get_retrieval_service] = override
    try:
        pid = _project(client)
        did = _upload(client, pid, "long.txt", "\n\n".join(f"{'word ' * 18}n{i}" for i in range(6)))
        assert _index(client, pid, did)["chunk_count"] >= 3
        result = _search(client, pid, query="word")
        assert result["scan_limited"] is True
        assert result["scanned_chunks"] == 2
    finally:
        app.dependency_overrides.pop(get_retrieval_service, None)


def test_tokenizer_and_query_parsing() -> None:
    assert tokenize("Caf\u00e9 NA\u00cfVE-test_1") == ["caf\u00e9", "na\u00efve", "test_1"]
    assert parse_query("b a b A") == ["b", "a"]


def test_foreign_chunk_row_cannot_cross_projects(
    client: TestClient, engine: Engine, small_chunks: None
) -> None:
    one, two = _project(client, "One"), _project(client, "Two")
    doc = _upload(client, one, "one.txt", "content")
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
    with Session(engine) as session:
        session.add(
            DocumentChunk(
                project_id=uuid.UUID(two),
                document_id=uuid.UUID(doc),
                chunk_index=0,
                text="content",
                source_start=0,
                source_end=7,
                chunker_version="x",
            )
        )
        with pytest.raises(Exception, match=r"(?i)foreign|constraint"):
            session.commit()
