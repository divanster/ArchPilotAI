from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.api import providers as providers_api
from app.api.deps import get_embedding_provider
from app.core.config import get_settings
from app.embeddings.fake import FakeEmbeddingProvider
from app.llm.contracts import LLMRequest, LLMResponse
from app.llm.fake_provider import FakeLLMProvider
from app.main import app
from app.mcp import tools
from app.mcp.tools import ToolContext
from app.services.agent import GRAPH_LABEL
from tests.test_agent import (
    action,
    clean_agent_state,
    context_of,
    finish,
    play,
    world,
    write,
)
from tests.test_patches import World, api

__all__ = ["clean_agent_state", "world"]


class RealStub(FakeEmbeddingProvider):
    """Stands in for a real provider: same vectors as the fake, but not flagged as a test one."""

    is_test_provider = False


def find(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = find(value, key)
            if found is not None:
                return found
    if isinstance(node, list):
        for item in node:
            found = find(item, key)
            if found is not None:
                return found
    return None


def upload_and_embed(client: TestClient, pid: str, text: str, embed: bool) -> None:
    response = client.post(
        f"/api/projects/{pid}/documents",
        files={"file": ("notes.txt", text.encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    did = response.json()["id"]
    assert client.post(f"/api/projects/{pid}/documents/{did}/index", json={}).status_code == 200
    if embed:
        app.dependency_overrides[get_embedding_provider] = lambda: FakeEmbeddingProvider()
        try:
            done = client.post(f"/api/projects/{pid}/documents/{did}/embed", json={})
        finally:
            app.dependency_overrides.pop(get_embedding_provider, None)
        assert done.status_code == 200, done.text


@pytest.fixture
def ctx(engine: Engine) -> ToolContext:
    return ToolContext(sessionmaker(bind=engine, expire_on_commit=False))


# ---- graph in the agent ------------------------------------------------------------------------


def test_run_context_has_bounded_related_items_from_the_graph(
    client: TestClient, world: World
) -> None:
    _run, provider = play(client, world, [finish()])
    related = find(context_of(provider.requests[0].prompt), GRAPH_LABEL)
    assert related is not None, "graph block missing from the agent context"
    assert related["items"], related
    assert len(related["items"]) <= 11
    assert all(len(i["affected"]) <= 8 for i in related["items"])
    assert all({"kind", "label"} <= set(a) for i in related["items"] for a in i["affected"])


def test_patch_records_requirements_and_tasks_touched_per_the_graph(
    client: TestClient, world: World
) -> None:
    run, _ = play(client, world, [write("src/new.py"), finish()])
    pid = world[0]
    patch = client.get(api(pid, f"/patches/{run['patch_id']}")).json()
    affected = patch["coverage"]["affected"]
    assert set(affected) >= {"requirements", "tasks"}
    task_requirements = {r for t in world[2].values() for r in t.get("requirement_ids", [])}
    assert affected["requirements"], affected
    assert {r["id"] for r in affected["requirements"]} <= task_requirements | {
        r["id"] for r in affected["requirements"]
    }
    plan_keys = set(world[2])
    assert {t["key"] for t in affected["tasks"]} <= plan_keys


def test_graph_context_is_scoped_to_the_project(
    client: TestClient, world: World, ctx: ToolContext
) -> None:
    other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    task = next(iter(world[2].values()))
    found = tools.graph_related(ctx, other, task["source_ref"], task.get("requirement_ids", []))
    assert found["available"] is False and found["roots"] == []


# ---- retrieval in the agent --------------------------------------------------------------------


def test_agent_search_falls_back_to_bm25_with_a_visible_note(
    client: TestClient, world: World
) -> None:
    pid = world[0]
    upload_and_embed(client, pid, "The zephyrquartz module handles billing exports.", embed=False)
    run, provider = play(
        client, world, [action("search_documents", query="zephyrquartz"), finish()]
    )
    step = run["steps"][0]
    assert step["tool"] == "search_documents" and step["tier"] == "read"
    assert step["result_summary"].endswith("bm25 (fallback)")
    result = context_of(provider.requests[1].prompt)["last_results"][-1]["result"]
    assert "BM25 results only" in str(result)


def test_test_embedding_provider_is_never_used_for_real_search(
    client: TestClient, world: World, ctx: ToolContext
) -> None:
    pid = world[0]
    upload_and_embed(client, pid, "The zephyrquartz module handles billing exports.", embed=True)
    found = tools.search_documents_auto(ctx, pid, "zephyrquartz", 5, FakeEmbeddingProvider())
    assert found["mode"] == "bm25" and "test provider" in found["note"]


def test_hybrid_only_with_a_real_provider_and_an_embedded_project(
    client: TestClient, world: World, ctx: ToolContext
) -> None:
    pid = world[0]
    upload_and_embed(client, pid, "The zephyrquartz module handles billing exports.", embed=True)
    found = tools.search_documents_auto(ctx, pid, "zephyrquartz", 5, RealStub())
    assert found["mode"] == "hybrid" and found["hits"]
    none = tools.search_documents_auto(ctx, pid, "zephyrquartz", 5, None)
    assert none["mode"] == "bm25" and "no embedding provider" in none["note"]


def test_real_provider_without_embeddings_falls_back_with_a_note(
    client: TestClient, world: World, ctx: ToolContext
) -> None:
    pid = world[0]
    upload_and_embed(client, pid, "The zephyrquartz module handles billing exports.", embed=False)
    found = tools.search_documents_auto(ctx, pid, "zephyrquartz", 5, RealStub())
    assert found["mode"] == "bm25" and "no embeddings" in found["note"]


def test_agent_search_is_scoped_to_the_project(
    client: TestClient, world: World, ctx: ToolContext
) -> None:
    other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    upload_and_embed(client, world[0], "The zephyrquartz module.", embed=False)
    assert tools.search_documents_auto(ctx, other, "zephyrquartz", 5, None)["hits"] == []


# ---- settings: providers and test connection ---------------------------------------------------


class CountingChat(FakeLLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return super().generate(request)


class CountingEmbedder(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def embed(self, texts: Any) -> Any:
        self.calls += 1
        return super().embed(texts)


@pytest.fixture
def counting(client: TestClient) -> Iterator[tuple[CountingChat, CountingEmbedder]]:
    chat, embedder = CountingChat(), CountingEmbedder()
    app.dependency_overrides[providers_api._chat_provider] = lambda: chat
    app.dependency_overrides[providers_api._embedding_provider] = lambda: embedder
    yield chat, embedder
    app.dependency_overrides.pop(providers_api._chat_provider, None)
    app.dependency_overrides.pop(providers_api._embedding_provider, None)


def test_listing_providers_never_calls_them(
    client: TestClient, counting: tuple[CountingChat, CountingEmbedder]
) -> None:
    chat, embedder = counting
    body = client.get("/api/settings/providers").json()
    assert [p["kind"] for p in body["providers"]] == ["chat", "embeddings"]
    assert (chat.calls, embedder.calls) == (0, 0)


def test_test_connection_runs_one_request_only_when_clicked(
    client: TestClient,
    counting: tuple[CountingChat, CountingEmbedder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, embedder = counting
    ok = client.post("/api/settings/providers/chat/test").json()
    assert ok["ok"] is True and chat.calls == 1 and embedder.calls == 0
    patched = get_settings().model_copy(update={"embedding_provider": "fake"})
    monkeypatch.setattr("app.api.providers.get_settings", lambda: patched)
    emb = client.post("/api/settings/providers/embeddings/test").json()
    assert emb["kind"] == "embeddings" and embedder.calls == 1 and chat.calls == 1


def test_status_values_and_no_secrets(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sekret-value-123"
    patched = get_settings().model_copy(
        update={
            "llm_provider": "kpmg_workbench",
            "embedding_provider": "none",
            "workbench_api_key": SecretStr(secret),
            "workbench_endpoint": "",
        }
    )
    monkeypatch.setattr("app.api.providers.get_settings", lambda: patched)
    response = client.get("/api/settings/providers")
    assert secret not in response.text
    by_kind = {p["kind"]: p for p in response.json()["providers"]}
    assert by_kind["chat"]["status"] == "not configured"
    assert by_kind["embeddings"]["status"] == "disabled"
    assert client.post("/api/settings/providers/chat/test").status_code == 409
    assert client.post("/api/settings/providers/embeddings/test").status_code == 409


def test_embedding_dimension_mismatch_is_reported(
    client: TestClient,
    counting: tuple[CountingChat, CountingEmbedder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = get_settings().model_copy(
        update={"embedding_provider": "fake", "embedding_dimension": 3072}
    )
    monkeypatch.setattr("app.api.providers.get_settings", lambda: patched)
    result = client.post("/api/settings/providers/embeddings/test").json()
    assert result["ok"] is False and "3072" in result["message"]
