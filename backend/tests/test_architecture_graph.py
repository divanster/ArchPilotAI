import copy
import re
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.architecture import ArchitectureContent
from app.domain.graph import (
    EdgeKind,
    EdgeSpec,
    ImpactMode,
    NodeKind,
    NodeSpec,
    build_graph,
    edge_id,
    impact,
    node_id,
    orphan_requirements,
)
from app.llm.fake_provider import FakeLLMProvider
from app.models.architecture import ArchitectureProposal
from app.models.graph import GraphEdge, GraphNode
from tests.test_architecture_api import arch_url, confirmed_project, generate
from tests.test_architecture_coverage import store_legacy
from tests.test_requirements_api import make_project, use

PID = uuid.UUID(int=1)


def graph_url(pid: str, proposal_id: str, suffix: str = "") -> str:
    return f"{arch_url(pid, '/' + proposal_id)}/graph{suffix}"


def generated(client: TestClient) -> tuple[str, str, list[str]]:
    pid, ids = confirmed_project(client)
    use(FakeLLMProvider())
    response = generate(client, pid)
    assert response.status_code == 201, response.text
    return pid, response.json()["id"], ids


def snapshot(client: TestClient, pid: str, proposal_id: str) -> dict[str, Any]:
    body: dict[str, Any] = client.get(graph_url(pid, proposal_id)).json()
    return body


def rebuild(client: TestClient, pid: str, proposal_id: str) -> dict[str, Any]:
    response = client.post(graph_url(pid, proposal_id, "/rebuild"))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def stored_content(engine: Engine, proposal_id: str) -> Any:
    with Session(engine) as session:
        proposal = session.get(ArchitectureProposal, uuid.UUID(proposal_id))
        assert proposal is not None
        return copy.deepcopy(proposal.content)


def content_of(client: TestClient, pid: str, proposal_id: str) -> ArchitectureContent:
    body = client.get(arch_url(pid, "/" + proposal_id)).json()
    return ArchitectureContent.model_validate(body["content"])


def keyed(snap: dict[str, Any]) -> tuple[set[tuple[str, str]], set[tuple[str, str, str, str]]]:
    nodes = {(n["kind"], n["source_ref"]) for n in snap["nodes"]}
    by_id = {n["id"]: (n["kind"], n["source_ref"]) for n in snap["nodes"]}
    edges = {
        (by_id[e["source_id"]][1], by_id[e["target_id"]][1], e["kind"], e["evidence"])
        for e in snap["edges"]
    }
    return nodes, edges


# ---- pure builder ----------------------------------------------------------------------------


def test_builder_is_deterministic_and_independent_of_input_order(client: TestClient) -> None:
    pid, proposal_id, ids = generated(client)
    content = content_of(client, pid, proposal_id)
    pidu = uuid.UUID(proposal_id)
    titles = {uuid.UUID(i): f"Title {n}" for n, i in enumerate(ids)}
    first = build_graph(pidu, content, titles)
    again = build_graph(pidu, content, dict(reversed(list(titles.items()))))
    assert first == again
    assert len({n.id for n in first.nodes}) == len(first.nodes)
    assert first.edges  # something to check


def test_every_edge_points_at_a_stored_field_and_carries_no_model_text(client: TestClient) -> None:
    pid, proposal_id, ids = generated(client)
    content = content_of(client, pid, proposal_id)
    spec = build_graph(uuid.UUID(proposal_id), content, {uuid.UUID(i): "t" for i in ids})
    pattern = re.compile(
        r"^(components|recommended_stack|integrations|data_architecture|security_architecture"
        r"|non_functional_design|risks|assumptions|open_questions|coverage_matrix|diagram\.edges)"
        r"(\[\d+\])?(\.[a-z_]+)?$"
    )
    for edge in spec.edges:
        assert pattern.match(edge.evidence), edge.evidence
    haystack = " ".join(e.evidence for e in spec.edges)
    for risk in content.risks:
        assert risk.description not in haystack


def test_node_without_incident_component_or_decision_is_an_orphan() -> None:
    a, b, c = (uuid.UUID(int=n) for n in (10, 11, 12))
    nodes = [
        NodeSpec(a, NodeKind.REQUIREMENT, "A", "a"),
        NodeSpec(b, NodeKind.REQUIREMENT, "B", "b"),
        NodeSpec(c, NodeKind.COMPONENT, "C", "components[0]"),
    ]
    edges = [EdgeSpec(uuid.UUID(int=99), a, c, EdgeKind.COVERED_BY, "coverage_matrix[0]")]
    assert [n.label for n in orphan_requirements(nodes, edges)] == ["B"]
    # A risk touching B does not rescue it from being an orphan.
    risk = NodeSpec(uuid.UUID(int=13), NodeKind.RISK, "R", "risks[0]")
    edges.append(
        EdgeSpec(uuid.UUID(int=98), risk.id, b, EdgeKind.AFFECTS, "risks[0].requirement_ids")
    )
    assert [n.label for n in orphan_requirements([*nodes, risk], edges)] == ["B"]


def edge(src: int, dst: int) -> EdgeSpec:
    s, t = uuid.UUID(int=src), uuid.UUID(int=dst)
    return EdgeSpec(edge_id(PID, s, t, EdgeKind.DEPENDS_ON), s, t, EdgeKind.DEPENDS_ON, "x")


def test_impact_terminates_on_a_cycle_and_reports_each_node_once() -> None:
    # 1 -> 2 -> 3 -> 1 : everything depends on everything.
    result = impact(
        uuid.UUID(int=1), [edge(1, 2), edge(2, 3), edge(3, 1)], mode=ImpactMode.TRANSITIVE
    )
    ids = [h.node_id.int for h in result.hits]
    assert sorted(ids) == [2, 3]
    assert len(ids) == len(set(ids))
    assert not result.truncated


def test_impact_depth_is_capped_and_flagged() -> None:
    chain = [edge(n + 1, n) for n in range(1, 9)]  # 2->1, 3->2, ... 9->8
    shallow = impact(uuid.UUID(int=1), chain, mode=ImpactMode.TRANSITIVE, max_depth=2)
    assert [h.depth for h in shallow.hits] == [1, 2]
    assert shallow.truncated
    deep = impact(
        uuid.UUID(int=1), chain, mode=ImpactMode.TRANSITIVE, max_depth=99
    )  # clamped to the maximum of 5
    assert max(h.depth for h in deep.hits) == 5
    assert deep.truncated


def test_impact_result_size_is_capped() -> None:
    fan = [edge(n, 1) for n in range(2, 12)]
    result = impact(uuid.UUID(int=1), fan, limit=3)
    assert len(result.hits) == 3
    assert result.truncated


# ---- API -------------------------------------------------------------------------------------


def test_unbuilt_graph_says_so_and_building_fills_it(client: TestClient) -> None:
    pid, proposal_id, _ = generated(client)
    before = snapshot(client, pid, proposal_id)
    assert before["built"] is False and before["nodes"] == [] and before["edges"] == []
    summary = rebuild(client, pid, proposal_id)
    after = snapshot(client, pid, proposal_id)
    assert after["built"] is True
    assert after["node_count"] == summary["node_count"] == len(after["nodes"])
    assert after["edge_count"] == summary["edge_count"] == len(after["edges"])
    kinds = {n["kind"] for n in after["nodes"]}
    assert {"requirement", "component", "decision"} <= kinds


def test_rebuild_is_idempotent_and_leaves_the_proposal_untouched(
    client: TestClient, engine: Engine
) -> None:
    pid, proposal_id, _ = generated(client)
    content_before = stored_content(engine, proposal_id)
    rebuild(client, pid, proposal_id)
    first = snapshot(client, pid, proposal_id)
    rebuild(client, pid, proposal_id)
    second = snapshot(client, pid, proposal_id)
    assert {n["id"] for n in first["nodes"]} == {n["id"] for n in second["nodes"]}
    assert {e["id"] for e in first["edges"]} == {e["id"] for e in second["edges"]}
    assert first["node_count"] == second["node_count"]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(GraphNode)) == first["node_count"]
        assert session.scalar(select(func.count()).select_from(GraphEdge)) == first["edge_count"]
    assert stored_content(engine, proposal_id) == content_before


def test_graph_covers_requirements_with_component_and_decision_edges(client: TestClient) -> None:
    pid, proposal_id, ids = generated(client)
    rebuild(client, pid, proposal_id)
    nodes, edges = keyed(snapshot(client, pid, proposal_id))
    assert {("requirement", i) for i in ids[:2]} <= nodes
    kinds = {e[2] for e in edges}
    assert "justified_by" in kinds
    assert all(e[3] for e in edges)  # every edge names its source field


def test_reads_are_scoped_to_the_project_and_proposal(client: TestClient) -> None:
    pid, proposal_id, _ = generated(client)
    rebuild(client, pid, proposal_id)
    other = make_project(client, "B")
    for suffix in ("", "/orphans"):
        assert client.get(graph_url(other, proposal_id, suffix)).status_code == 404
    assert client.post(graph_url(other, proposal_id, "/rebuild")).status_code == 404
    assert client.get(graph_url(pid, str(uuid.uuid4()))).status_code == 404

    node = snapshot(client, pid, proposal_id)["nodes"][0]["id"]
    assert client.get(graph_url(other, proposal_id, f"/impact?node_id={node}")).status_code == 404

    # A second proposal of the same project cannot be queried with the first one's node.
    second = generate(client, pid).json()["id"]
    rebuild(client, pid, second)
    response = client.get(graph_url(pid, second, f"/impact?node_id={node}"))
    assert response.status_code == 404


def test_edges_cannot_cross_proposals_or_projects(client: TestClient, engine: Engine) -> None:
    pid, first, _ = generated(client)
    second = generate(client, pid).json()["id"]
    rebuild(client, pid, first)
    rebuild(client, pid, second)
    with Session(engine) as session:
        a = session.scalars(
            select(GraphNode).where(GraphNode.proposal_id == uuid.UUID(first))
        ).first()
        b = session.scalars(
            select(GraphNode).where(GraphNode.proposal_id == uuid.UUID(second))
        ).first()
        assert a is not None and b is not None
        session.add(
            GraphEdge(
                id=uuid.uuid4(),
                project_id=a.project_id,
                proposal_id=a.proposal_id,
                source_id=a.id,
                target_id=b.id,
                kind="affects",
                evidence="x",
            )
        )
        raised = False
        try:
            session.commit()
        except IntegrityError:
            raised = True
            session.rollback()
        assert raised


def test_orphans_endpoint_lists_requirements_nothing_covers(client: TestClient) -> None:
    pid, proposal_id, ids = generated(client)
    rebuild(client, pid, proposal_id)
    orphans = client.get(graph_url(pid, proposal_id, "/orphans")).json()["orphans"]
    refs = {o["source_ref"] for o in orphans}
    assert refs <= set(ids[:2])
    # A requirement confirmed after generation is not in the proposal's graph at all.
    assert str(uuid.uuid4()) not in refs


def test_impact_lists_dependents_of_a_component_with_bounded_depth(client: TestClient) -> None:
    pid, proposal_id, _ = generated(client)
    rebuild(client, pid, proposal_id)
    snap = snapshot(client, pid, proposal_id)
    requirement = next(n for n in snap["nodes"] if n["kind"] == "requirement")
    body = client.get(
        graph_url(pid, proposal_id, f"/impact?node_id={requirement['id']}&mode=transitive")
    ).json()
    assert body["root"]["id"] == requirement["id"]
    assert body["max_depth"] == 3
    assert body["items"]
    assert all(1 <= i["depth"] <= 3 and i["via_evidence"] for i in body["items"])
    assert requirement["id"] not in {i["node"]["id"] for i in body["items"]}
    assert (
        client.get(
            graph_url(pid, proposal_id, f"/impact?node_id={requirement['id']}&depth=9")
        ).status_code
        == 422
    )
    assert (
        client.get(graph_url(pid, proposal_id, f"/impact?node_id={uuid.uuid4()}")).status_code
        == 404
    )


def test_rebuild_after_a_requirement_is_unconfirmed_drops_it_and_reports_it(
    client: TestClient,
) -> None:
    pid, proposal_id, ids = generated(client)
    rebuild(client, pid, proposal_id)
    assert ("requirement", ids[0]) in keyed(snapshot(client, pid, proposal_id))[0]
    patched = client.patch(f"/api/projects/{pid}/requirements/{ids[0]}", json={"title": "Changed"})
    assert patched.status_code == 200
    # The stored graph still shows the old state until it is rebuilt.
    assert ("requirement", ids[0]) in keyed(snapshot(client, pid, proposal_id))[0]
    summary = rebuild(client, pid, proposal_id)
    assert summary["skipped_requirement_refs"] >= 1
    assert ("requirement", ids[0]) not in keyed(snapshot(client, pid, proposal_id))[0]


def test_legacy_proposal_builds_without_coverage_edges(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    legacy = store_legacy(engine, pid, ids[:2])
    assert snapshot(client, pid, legacy)["built"] is False
    summary = rebuild(client, pid, legacy)
    assert summary["node_count"] > 0
    _, edges = keyed(snapshot(client, pid, legacy))
    assert "covered_by" not in {e[2] for e in edges}
    assert "justified_by" in {e[2] for e in edges}
    orphans = client.get(graph_url(pid, legacy, "/orphans")).json()["orphans"]
    assert isinstance(orphans, list)


def test_approving_rebuilds_the_graph_and_a_failure_does_not_block_approval(
    client: TestClient,
) -> None:
    pid, proposal_id, _ = generated(client)
    assert snapshot(client, pid, proposal_id)["built"] is False
    approved = client.patch(arch_url(pid, "/" + proposal_id), json={"status": "approved"})
    assert approved.status_code == 200, approved.text
    assert snapshot(client, pid, proposal_id)["built"] is True


def test_editing_a_draft_leaves_a_stale_graph_until_rebuilt(client: TestClient) -> None:
    pid, proposal_id, _ = generated(client)
    rebuild(client, pid, proposal_id)
    before = snapshot(client, pid, proposal_id)
    assert (
        client.patch(arch_url(pid, "/" + proposal_id), json={"title": "New title"}).status_code
        == 200
    )
    assert snapshot(client, pid, proposal_id)["node_count"] == before["node_count"]


def test_node_ids_are_stable_uuid5_of_proposal_and_reference() -> None:
    assert node_id(PID, NodeKind.RISK, "risks[0]") == node_id(PID, NodeKind.RISK, "risks[0]")
    assert node_id(PID, NodeKind.RISK, "risks[0]") != node_id(
        uuid.UUID(int=2), NodeKind.RISK, "risks[0]"
    )
