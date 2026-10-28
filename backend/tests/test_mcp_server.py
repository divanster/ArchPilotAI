import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.mcp import tools
from app.mcp.server import create_server
from app.mcp.tools import ReadOnlyViolationError, ToolContext, ToolInputError, read_only_session
from app.models.architecture import ArchitectureProposal
from app.models.base import Base
from app.models.project import Project
from tests.test_architecture_api import arch_url
from tests.test_architecture_graph import generated, graph_url
from tests.test_requirements_api import make_project

SECRET_WORD = "zanzibarquartz"


def upload_and_index(client: TestClient, pid: str, text: str, name: str = "notes.txt") -> str:
    response = client.post(
        f"/api/projects/{pid}/documents", files={"file": (name, text.encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text
    did = str(response.json()["id"])
    indexed = client.post(f"/api/projects/{pid}/documents/{did}/index", json={"reindex": False})
    assert indexed.status_code == 200, indexed.text
    return did


class World:
    def __init__(self, ctx: ToolContext, pid: str, other: str, proposal_id: str) -> None:
        self.ctx = ctx
        self.pid = pid
        self.other = other
        self.proposal_id = proposal_id
        self.nodes: list[dict[str, Any]] = []


@pytest.fixture
def world(client: TestClient, engine: Engine) -> World:
    pid, proposal_id, _ids = generated(client)
    approved = client.patch(arch_url(pid, "/" + proposal_id), json={"status": "approved"})
    assert approved.status_code == 200, approved.text
    other = make_project(client, "Other")
    upload_and_index(client, other, f"The {SECRET_WORD} subsystem handles billing.")
    upload_and_index(client, pid, "The platform shall export reports as PDF every night.")
    ctx = ToolContext(sessionmaker(bind=engine, expire_on_commit=False))
    w = World(ctx, pid, other, proposal_id)
    w.nodes = client.get(graph_url(pid, proposal_id)).json()["nodes"]
    return w


def run(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return call()


def test_list_projects(world: World) -> None:
    result = tools.list_projects(world.ctx)
    ids = {p["project_id"] for p in result["projects"]}
    assert {world.pid, world.other} <= ids
    assert result["truncated"] is False


def test_list_projects_truncates(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "MAX_PROJECTS", 1)
    result = tools.list_projects(world.ctx)
    assert len(result["projects"]) == 1
    assert result["total"] >= 2
    assert result["truncated"] is True


def test_get_requirements_with_sources(world: World) -> None:
    confirmed = tools.get_requirements(world.ctx, world.pid)
    assert confirmed["status"] == "confirmed"
    assert confirmed["total"] == 2 == len(confirmed["requirements"])
    source = confirmed["requirements"][0]["source"]
    assert set(source) == {"document_id", "source_start", "source_end", "quote"}
    assert source["source_end"] > source["source_start"] >= 0
    proposed = tools.get_requirements(world.ctx, world.pid, "proposed")
    assert all(r["status"] == "proposed" for r in proposed["requirements"])
    with pytest.raises(ToolInputError, match="status must be one of"):
        tools.get_requirements(world.ctx, world.pid, "bogus")


def test_get_requirements_truncates(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "MAX_REQUIREMENTS", 1)
    result = tools.get_requirements(world.ctx, world.pid)
    assert len(result["requirements"]) == 1
    assert result["total"] == 2
    assert result["truncated"] is True


def test_get_architecture_latest_approved_and_version(world: World) -> None:
    result = tools.get_architecture(world.ctx, world.pid)
    assert result["proposal_id"] == world.proposal_id
    assert result["status"] == result["approval_status"] == "approved"
    assert "content" not in result
    assert "components" in result["sections_index"]
    assert result["truncated"] is False
    assert result["truncated_sections"] == {}
    assert (
        tools.get_architecture(world.ctx, world.pid, result["version"])["version"]
        == (result["version"])
    )
    with pytest.raises(ToolInputError, match="no architecture version 99"):
        tools.get_architecture(world.ctx, world.pid, 99)
    with pytest.raises(ToolInputError, match="no approved"):
        tools.get_architecture(world.ctx, world.other)


def test_get_architecture_index_counts_match_sections(world: World) -> None:
    index = tools.get_architecture(world.ctx, world.pid)["sections_index"]
    full = tools.get_architecture(world.ctx, world.pid, sections=list(index))["content"]
    assert list(full) == list(index)
    for name, value in full.items():
        if isinstance(value, list):
            assert index[name]["items"] == len(value)


def test_get_architecture_lint_findings_present(world: World) -> None:
    result = tools.get_architecture(world.ctx, world.pid)
    assert "lint_findings" in result
    assert result["lint_findings"] is None or "findings" in result["lint_findings"]


def test_get_architecture_lint_findings_null_when_never_linted(
    world: World, engine: Engine
) -> None:
    with Session(engine) as session:
        proposal = session.get(ArchitectureProposal, uuid.UUID(world.proposal_id))
        assert proposal is not None
        proposal.lint_findings = None
        session.commit()
    assert tools.get_architecture(world.ctx, world.pid)["lint_findings"] is None


def test_get_architecture_sections_selection(world: World) -> None:
    result = tools.get_architecture(
        world.ctx, world.pid, sections=["recommended_stack", "components"]
    )
    assert list(result["content"]) == ["recommended_stack", "components"]
    assert "sections_index" not in result


def test_get_architecture_sections_order_is_stable(world: World) -> None:
    a = tools.get_architecture(world.ctx, world.pid, sections=["components", "recommended_stack"])
    b = tools.get_architecture(
        world.ctx, world.pid, sections=["recommended_stack", "components", "components"]
    )
    assert a["content"] == b["content"]
    assert list(a["content"]) == list(b["content"])


def test_get_architecture_unknown_section(world: World) -> None:
    with pytest.raises(ToolInputError, match="Unknown section.*nope.*Valid sections:.*components"):
        tools.get_architecture(world.ctx, world.pid, sections=["components", "nope"])


def test_get_architecture_reports_truncation_per_section(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tools.get_architecture(world.ctx, world.pid)["sections_index"]
    name = next(n for n, v in index.items() if (v["items"] or 0) > 1)
    total = index[name]["items"]
    monkeypatch.setattr(tools, "MAX_SECTION_ITEMS", 1)
    result = tools.get_architecture(world.ctx, world.pid, sections=[name])
    assert len(result["content"][name]) == 1
    assert result["truncated"] is True
    assert result["truncated_sections"][name] == {"returned": 1, "total": total}


def test_get_architecture_size_budget_reports_cut(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = tools.get_architecture(world.ctx, world.pid)["sections_index"]
    name = next(n for n, v in index.items() if (v["items"] or 0) > 1)
    monkeypatch.setattr(tools, "MAX_ARCHITECTURE_CHARS", 10)
    result = tools.get_architecture(world.ctx, world.pid, sections=[name])
    report = result["truncated_sections"][name]
    assert report["total"] == index[name]["items"]
    assert report["returned"] == len(result["content"][name]) < report["total"]
    assert result["truncated"] is True


def test_get_architecture_truncates_strings(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "MAX_STRING_CHARS", 20)
    result = tools.get_architecture(world.ctx, world.pid, sections=["solution_overview"])
    assert len(result["content"]["solution_overview"]) <= 20
    assert result["truncated"] is True


def test_requirement_code_derivation(world: World) -> None:
    assert tools._requirement_code("REQ-001: The system shall") == "REQ-001"
    assert tools._requirement_code("NFR-12: too short") is None
    assert tools._requirement_code("see REQ-001: text") is None
    assert tools._requirement_code("req-001: lowercase") is None
    assert tools._requirement_code("ABCDE-001: too long") is None
    items = tools.get_requirements(world.ctx, world.pid)["requirements"]
    assert all("code" in r for r in items)
    assert all(r["code"] is None for r in items)


def test_get_impact_direct_and_transitive(world: World) -> None:
    requirement = next(n for n in world.nodes if n["kind"] == "requirement")
    direct = tools.get_impact(world.ctx, world.pid, requirement["id"])
    assert direct["mode"] == "direct"
    assert direct["root"]["node_id"] == requirement["id"]
    assert direct["impacted"]
    assert {"source_ref", "via_evidence", "depth"} <= set(direct["impacted"][0])
    transitive = tools.get_impact(world.ctx, world.pid, requirement["id"], "transitive")
    assert transitive["total"] >= direct["total"]
    with pytest.raises(ToolInputError, match="mode must be"):
        tools.get_impact(world.ctx, world.pid, requirement["id"], "deep")


def test_get_impact_truncates(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "MAX_IMPACT_HITS", 0)
    requirement = next(n for n in world.nodes if n["kind"] == "requirement")
    result = tools.get_impact(world.ctx, world.pid, requirement["id"])
    assert result["impacted"] == []
    assert result["truncated"] is True


def test_get_orphans(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    result = tools.get_orphans(world.ctx, world.pid)
    assert result["total"] == len(result["orphan_requirements"])
    assert result["truncated"] is False
    with pytest.raises(ToolInputError, match="no approved"):
        tools.get_orphans(world.ctx, world.other)


def test_search_documents_and_isolation(world: World) -> None:
    found = tools.search_documents(world.ctx, world.other, SECRET_WORD)
    assert len(found["hits"]) == 1
    hit = found["hits"][0]
    assert {"document_id", "filename", "source_start", "source_end"} <= set(hit)
    assert SECRET_WORD in hit["text"]
    leak = tools.search_documents(world.ctx, world.pid, SECRET_WORD)
    assert leak["hits"] == []
    assert tools.search_documents(world.ctx, world.pid, "reports PDF")["hits"]


def test_search_truncates_and_bounds(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "MAX_STRING_CHARS", 5)
    result = tools.search_documents(world.ctx, world.other, SECRET_WORD, top_k=1000)
    assert result["truncated"] is True
    assert len(result["hits"][0]["text"]) == 5
    with pytest.raises(ToolInputError):
        tools.search_documents(world.ctx, world.other, "x" * 500)


def test_unknown_and_invalid_ids(world: World) -> None:
    unknown = str(uuid.uuid4())
    calls: list[Callable[[], Any]] = [
        lambda: tools.get_requirements(world.ctx, unknown),
        lambda: tools.get_architecture(world.ctx, unknown),
        lambda: tools.get_impact(world.ctx, unknown, unknown),
        lambda: tools.get_orphans(world.ctx, unknown),
        lambda: tools.search_documents(world.ctx, unknown, "q"),
    ]
    for call in calls:
        with pytest.raises(ToolInputError, match="Unknown project_id"):
            call()
    with pytest.raises(ToolInputError, match="must be a UUID"):
        tools.get_requirements(world.ctx, "not-a-uuid")
    with pytest.raises(ToolInputError, match="Unknown node_id"):
        tools.get_impact(world.ctx, world.pid, unknown)


def test_node_of_another_project_is_unknown(world: World, client: TestClient) -> None:
    foreign = next(n for n in world.nodes if n["kind"] == "requirement")["id"]
    with pytest.raises(ToolInputError):
        tools.get_impact(world.ctx, world.other, foreign)


def dump(engine: Engine) -> dict[str, list[str]]:
    state: dict[str, list[str]] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.execute(select(table)).all()
            state[table.name] = sorted(str(r) for r in rows)
    return state


def test_no_tool_modifies_the_database(world: World, engine: Engine) -> None:
    before = dump(engine)
    node = next(n for n in world.nodes if n["kind"] == "requirement")["id"]
    for pid in (world.pid, world.other):
        tools.get_requirements(world.ctx, pid)
        tools.search_documents(world.ctx, pid, "platform")
        for fn in (tools.get_architecture, tools.get_orphans):
            with contextlib.suppress(ToolInputError):
                fn(world.ctx, pid)
    tools.get_architecture(world.ctx, world.pid, sections=["components", "coverage_matrix"])
    tools.list_projects(world.ctx)
    tools.get_impact(world.ctx, world.pid, node, "transitive")
    assert dump(engine) == before


def test_session_refuses_writes(world: World, engine: Engine) -> None:
    with read_only_session(world.ctx) as session, pytest.raises(ReadOnlyViolationError):
        session.add(Project(name="sneaky"))
        session.flush()
    with Session(engine) as check:
        assert check.scalars(select(Project).where(Project.name == "sneaky")).first() is None


def test_server_exposes_only_read_tools(world: World) -> None:
    server = create_server(world.ctx)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "list_projects",
        "get_requirements",
        "get_architecture",
        "get_plan",
        "get_patch",
        "get_workspace_tree",
        "get_impact",
        "get_orphans",
        "search_documents",
    }
    assert not any(
        verb in n for n in names for verb in ("create", "update", "delete", "set_", "approve")
    )


def test_server_call_returns_json_and_clear_errors(world: World) -> None:
    server = create_server(world.ctx)
    result = asyncio.run(server.call_tool("get_requirements", {"project_id": world.pid}))
    text = json.dumps(result, default=str)
    assert world.pid in text
    with pytest.raises(ToolError, match="Unknown project_id"):
        asyncio.run(server.call_tool("get_requirements", {"project_id": str(uuid.uuid4())}))


def test_logging_has_name_and_duration_but_no_document_text(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    server = create_server(world.ctx)
    with caplog.at_level(logging.INFO, logger="archpilot.mcp"):
        asyncio.run(
            server.call_tool("search_documents", {"project_id": world.other, "query": SECRET_WORD})
        )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "tool=search_documents" in logged
    assert "duration_ms=" in logged
    assert SECRET_WORD not in logged
    assert "subsystem handles billing" not in logged
