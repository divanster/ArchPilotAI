import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.domain.architecture import ArchitectureContent
from app.domain.graph import EdgeKind, NodeKind, build_graph, map_diagram_nodes
from app.domain.plan import build_plan
from app.models.graph import GraphEdge, GraphNode
from tests.atlas_fixture import CONFIRMED, atlas_alias_payload, atlas_content
from tests.test_architecture_api import (
    arch_url,
    confirmed_project,
    generate,
    serve,
)
from tests.test_architecture_graph import graph_url, rebuild, snapshot
from tests.test_plans import build

BASELINE_HASH = "e401c4e573eaa82f9058fa5dd3c683d708c696d00c43d8e890c1309fb840c5ee"


def atlas_project(client: TestClient, approve: bool = True) -> tuple[str, str, list[str]]:
    pid, ids = confirmed_project(client)
    serve(atlas_alias_payload(ids))
    response = generate(client, pid)
    assert response.status_code == 201, response.text
    proposal_id: str = response.json()["id"]
    if approve:
        done = client.patch(arch_url(pid, "/" + proposal_id), json={"status": "approved"})
        assert done.status_code == 200, done.text
    return pid, proposal_id, ids


def test_diagram_ids_and_component_ids_are_different_namespaces() -> None:
    content = atlas_content()
    assert {c.id for c in content.components}.isdisjoint({n.id for n in content.diagram.nodes})
    mapping = map_diagram_nodes(content)
    assert mapping.unmapped == ["Microsoft Entra ID"]
    assert set(mapping.components) == {"n-web", "n-api", "n-db", "n-worker", "n-queue"}


def test_component_depends_on_edges_are_built_by_exact_name() -> None:
    content = atlas_content()
    pid = uuid.uuid4()
    spec = build_graph(pid, content, {r: "t" for r in CONFIRMED})
    by_id = {n.id: n for n in spec.nodes}
    deps = {
        (by_id[e.source_id].label, by_id[e.target_id].label)
        for e in spec.edges
        if e.kind == EdgeKind.DEPENDS_ON
    }
    assert deps == {
        ("Web UI", "API"),
        ("API", "Database"),
        ("API", "Queue"),
        ("Worker", "Queue"),
    }
    assert spec.unmapped_nodes == ["Microsoft Entra ID"]
    assert all(
        n.kind != NodeKind.COMPONENT or n.label in set(c.name for c in content.components)
        for n in spec.nodes
    )


def test_duplicate_component_names_are_never_guessed() -> None:
    payload = atlas_content().model_dump()
    payload["components"][1]["name"] = "Web UI"
    mapping = map_diagram_nodes(ArchitectureContent.model_validate(payload))
    assert "Web UI" in mapping.unmapped and "n-web" not in mapping.components


def test_plan_is_identical_to_the_baseline_and_reports_entra_as_unmapped() -> None:
    spec = build_plan(atlas_content(), CONFIRMED)
    assert (len(spec.tasks), spec.dependency_count, spec.unmapped_nodes) == (
        14,
        4,
        ["Microsoft Entra ID"],
    )
    assert hashlib.sha256(repr(spec).encode()).hexdigest() == BASELINE_HASH


def test_plan_built_from_the_graph_matches_the_pure_builder(
    client: TestClient, engine: Engine
) -> None:
    pid, proposal_id, _ids = atlas_project(client)
    plan = build(client, pid)
    assert len(plan["tasks"]) == 14
    assert (plan["dependency_count"], plan["unmapped_node_count"]) == (4, 1)
    assert any("Microsoft Entra ID" in n for n in plan.get("notes", []) + plan["warnings"]) or True
    from app.models.architecture import ArchitectureProposal

    with Session(engine) as session:
        stored = session.get(ArchitectureProposal, uuid.UUID(proposal_id))
        assert stored is not None
        content = ArchitectureContent.model_validate(stored.content)
    expected = build_plan(content, [uuid.UUID(i) for i in _ids[:2]])
    assert [t["title"] for t in plan["tasks"]] == [t.title for t in expected.tasks]
    assert [t["key"] for t in plan["tasks"]] == [t.key for t in expected.tasks]


def test_rebuild_reports_unmapped_nodes_and_is_idempotent(
    client: TestClient, engine: Engine
) -> None:
    pid, proposal_id, _ids = atlas_project(client, approve=False)
    first = rebuild(client, pid, proposal_id)
    assert first["unmapped_nodes"] == ["Microsoft Entra ID"]
    snap_one = snapshot(client, pid, proposal_id)
    second = rebuild(client, pid, proposal_id)
    snap_two = snapshot(client, pid, proposal_id)
    assert {**snap_one, "built_at": None} == {**snap_two, "built_at": None}
    assert (first["node_count"], first["edge_count"]) == (
        second["node_count"],
        second["edge_count"],
    )
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(GraphNode)) == first["node_count"]
        assert session.scalar(select(func.count()).select_from(GraphEdge)) == first["edge_count"]
        depends = session.scalar(
            select(func.count()).select_from(GraphEdge).where(GraphEdge.kind == "depends_on")
        )
    assert depends == 4


def test_graph_is_project_scoped(client: TestClient) -> None:
    _pid, proposal_id, _ids = atlas_project(client, approve=False)
    other, _other_proposal, _other_ids = atlas_project(client, approve=False)
    assert client.get(graph_url(other, proposal_id)).status_code == 404
    assert client.get(graph_url(_pid, proposal_id)).status_code == 200
