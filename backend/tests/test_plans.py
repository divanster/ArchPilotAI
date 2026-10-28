import copy
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.architecture import ArchitectureContent
from app.domain.plan import (
    AI_RECOMMENDATION_FLAG,
    NO_COVERAGE_FLAG,
    PlanCycleError,
    PlanSpec,
    build_plan,
)
from app.mcp import tools
from app.mcp.tools import ToolContext, ToolInputError
from app.models.graph import GraphEdge, GraphNode
from app.models.plan import ImplementationPlan, PlanTask
from tests.test_architecture_api import (
    alias_payload,
    arch_url,
    confirmed_project,
    generate,
    serve,
    valid_payload,
)
from tests.test_requirements_api import make_project

Mutator = Callable[[dict[str, Any]], None]


def plans_url(pid: str, suffix: str = "") -> str:
    return f"/api/projects/{pid}/plans{suffix}"


def payload_for(ids: list[str], mutate: Mutator | None = None) -> dict[str, Any]:
    payload = copy.deepcopy(valid_payload(ids))
    if mutate:
        mutate(payload)
    return payload


def content_for(ids: list[str], mutate: Mutator | None = None) -> ArchitectureContent:
    payload = payload_for(ids, mutate)
    payload.pop("title", None)
    payload.pop("summary", None)
    return ArchitectureContent.model_validate(payload)


FAKE_IDS = [str(uuid.UUID(int=n)) for n in (101, 102)]
CONFIRMED = [uuid.UUID(i) for i in FAKE_IDS]


def plan_of(mutate: Mutator | None = None, confirmed: list[uuid.UUID] | None = None) -> PlanSpec:
    return build_plan(content_for(FAKE_IDS, mutate), CONFIRMED if confirmed is None else confirmed)


def titles(spec: PlanSpec) -> list[str]:
    return [t.title for t in spec.tasks]


def by_title(spec: PlanSpec, title: str) -> Any:
    return next(t for t in spec.tasks if t.title == title)


def approved_project(
    client: TestClient, mutate: Mutator | None = None
) -> tuple[str, str, list[str]]:
    pid, ids = confirmed_project(client)
    payload = alias_payload(ids)
    if mutate:
        mutate(payload)
    serve(payload)
    response = generate(client, pid)
    assert response.status_code == 201, response.text
    proposal_id: str = response.json()["id"]
    approved = client.patch(arch_url(pid, "/" + proposal_id), json={"status": "approved"})
    assert approved.status_code == 200, approved.text
    return pid, proposal_id, ids


def build(client: TestClient, pid: str) -> dict[str, Any]:
    response = client.post(plans_url(pid))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ---- pure builder ----------------------------------------------------------------------------


def test_one_task_per_component_and_decision_in_dependency_order() -> None:
    spec = plan_of()
    assert titles(spec) == [
        "Implement Database",
        "Implement Identity provider",
        "Implement API",
        "Implement Web UI",
        "Integrate: Identity",
        "Secure: Access control",
        "NFR: Performance",
    ]
    assert [t.key for t in spec.tasks] == [f"T-{n:03d}" for n in range(1, 8)]
    assert [t.order_index for t in spec.tasks] == list(range(1, 8))
    keys = {t.title: t.key for t in spec.tasks}
    assert by_title(spec, "Implement Web UI").depends_on == [keys["Implement API"]]
    assert by_title(spec, "Implement API").depends_on == sorted(
        [keys["Implement Database"], keys["Implement Identity provider"]]
    )
    assert by_title(spec, "Implement Database").depends_on == []
    assert by_title(spec, "Implement Database").kind == "data"
    assert by_title(spec, "Implement Web UI").kind == "component"
    assert [t.kind for t in spec.tasks[-3:]] == ["integration", "security", "nfr"]
    position = {t.key: t.order_index for t in spec.tasks}
    assert all(position[d] < t.order_index for t in spec.tasks for d in t.depends_on)


def test_order_is_deterministic_and_independent_of_edge_and_coverage_order() -> None:
    def shuffle(p: dict[str, Any]) -> None:
        p["diagram"]["edges"].reverse()
        p["coverage_matrix"].reverse()

    assert plan_of(shuffle) == plan_of()
    graph_deps = [("components[1]", "components[2]"), ("components[0]", "components[1]")]
    content = content_for(FAKE_IDS)
    assert build_plan(content, CONFIRMED, graph_deps) == build_plan(
        content, CONFIRMED, reversed(graph_deps)
    )


def test_independent_tasks_follow_declaration_order() -> None:
    def no_edges(p: dict[str, Any]) -> None:
        p["diagram"]["edges"] = []

    spec = plan_of(no_edges)
    assert titles(spec)[:4] == [
        "Implement Web UI",
        "Implement API",
        "Implement Database",
        "Implement Identity provider",
    ]
    assert all(t.depends_on == [] for t in spec.tasks)


def test_graph_dependencies_are_used_too() -> None:
    def no_edges(p: dict[str, Any]) -> None:
        p["diagram"]["edges"] = []

    spec = build_plan(
        content_for(FAKE_IDS, no_edges), CONFIRMED, [("components[0]", "components[3]")]
    )
    assert titles(spec).index("Implement Identity provider") < titles(spec).index(
        "Implement Web UI"
    )


def test_dependency_cycle_fails_and_is_reported() -> None:
    def cycle(p: dict[str, Any]) -> None:
        p["diagram"]["edges"].append({"source": "db", "target": "web", "relationship": "oops"})

    with pytest.raises(PlanCycleError) as info:
        plan_of(cycle)
    assert info.value.cycle[0] == info.value.cycle[-1]
    assert {"web", "api", "db"} <= set(info.value.cycle)
    assert "idp" not in info.value.cycle
    assert "cycle" in str(info.value)


def test_requirement_links_never_invent_requirements() -> None:
    spec = plan_of()
    assert all(set(t.requirement_ids) <= set(CONFIRMED) for t in spec.tasks)
    assert by_title(spec, "Integrate: Identity").requirement_ids == []  # assumed, no evidence
    assert by_title(spec, "Secure: Access control").requirement_ids == [CONFIRMED[0]]

    # A requirement that is no longer confirmed is dropped and said so.
    spec = plan_of(confirmed=[CONFIRMED[0]])
    assert all(CONFIRMED[1] not in t.requirement_ids for t in spec.tasks)
    api = by_title(spec, "Implement API")
    assert "1 linked requirement(s) are no longer confirmed" in api.flags


def test_coverage_matrix_links_requirements_to_decisions() -> None:
    def link(p: dict[str, Any]) -> None:
        p["coverage_matrix"][1]["related_ids"] = ["api", "d-identity"]

    task = by_title(plan_of(link), "Integrate: Identity")
    assert task.requirement_ids == [CONFIRMED[1]]
    assert task.acceptance_criteria  # copied from that requirement's coverage entry


def test_partial_coverage_is_marked_needs_clarification_with_its_gap() -> None:
    def mixed(p: dict[str, Any]) -> None:
        entry = p["coverage_matrix"][1]
        entry["status"] = "addressed"
        entry["gap"] = None
        entry["verification"] = "Run the export end to end."

    task = by_title(plan_of(mixed), "Implement API")
    partial, addressed = (
        next(c for c in task.acceptance_criteria if "reviewed with the client" in c),
        next(c for c in task.acceptance_criteria if "export" in c),
    )
    assert "needs clarification" in partial
    assert "Needs human review of how the design satisfies it." in partial
    assert addressed == "Run the export end to end."


def test_missing_coverage_matrix_is_flagged_and_has_no_criteria() -> None:
    def legacy(p: dict[str, Any]) -> None:
        p["coverage_matrix"] = None

    spec = plan_of(legacy)
    assert all(NO_COVERAGE_FLAG in t.flags and t.acceptance_criteria == [] for t in spec.tasks)


def test_ai_recommendations_are_flagged() -> None:
    spec = plan_of()
    assert AI_RECOMMENDATION_FLAG in by_title(spec, "Secure: Access control").flags
    assert AI_RECOMMENDATION_FLAG in by_title(spec, "NFR: Performance").flags
    assert AI_RECOMMENDATION_FLAG not in by_title(spec, "Integrate: Identity").flags
    assert AI_RECOMMENDATION_FLAG not in by_title(spec, "Implement API").flags


def test_open_questions_block_only_the_tasks_they_affect() -> None:
    def question(p: dict[str, Any]) -> None:
        p["open_questions"] = [
            {
                "question": "Which retention period applies?",
                "why_it_matters": "It sizes storage.",
                "requirement_ids": [FAKE_IDS[1]],
            },
            {"question": "Who owns operations?", "requirement_ids": []},
        ]

    spec = plan_of(question)
    blocked = {t.title for t in spec.tasks if t.blocking_questions}
    assert blocked == {
        "Implement Web UI",
        "Implement API",
        "Implement Database",
        "Implement Identity provider",
    }
    assert by_title(spec, "Implement API").blocking_questions == [
        "Which retention period applies? — It sizes storage."
    ]
    assert by_title(spec, "Secure: Access control").blocking_questions == []
    assert spec.unattached_questions == ["Who owns operations?"]


# ---- database --------------------------------------------------------------------------------


def test_composite_keys_keep_plans_and_tasks_in_their_project(
    client: TestClient, engine: Engine
) -> None:
    pid, proposal_id, _ = approved_project(client)
    plan = build(client, pid)
    other = make_project(client, "Other")
    with Session(engine) as session:
        session.add(
            PlanTask(
                plan_id=uuid.UUID(plan["id"]),
                project_id=uuid.UUID(other),
                key="T-900",
                title="x",
                kind="setup",
                description="x",
                source_ref="x",
                order_index=900,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    with Session(engine) as session:
        session.add(
            ImplementationPlan(
                project_id=uuid.UUID(other), proposal_id=uuid.UUID(proposal_id), version=1
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# ---- API -------------------------------------------------------------------------------------


def test_build_stores_an_ordered_draft_plan(client: TestClient, engine: Engine) -> None:
    pid, proposal_id, ids = approved_project(client)
    plan = build(client, pid)
    assert (plan["status"], plan["version"], plan["proposal_id"]) == ("draft", 1, proposal_id)
    assert plan["task_count"] == len(plan["tasks"]) == 7
    assert plan["source_graph_built_at"] is not None
    assert [t["key"] for t in plan["tasks"]] == [f"T-{n:03d}" for n in range(1, 8)]
    assert all(t["status"] == "todo" for t in plan["tasks"])
    assert {r for t in plan["tasks"] for r in t["requirement_ids"]} <= set(ids)
    assert plan["unattached_questions"] == [
        "Which identity provider does the client use? — It decides the authentication design."
    ]
    assert client.get(plans_url(pid, "/" + plan["id"])).json() == plan
    listed = client.get(plans_url(pid)).json()
    assert [(p["id"], p["task_count"]) for p in listed] == [(plan["id"], 7)]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PlanTask)) == 7


def test_no_plan_without_an_approved_proposal(client: TestClient, engine: Engine) -> None:
    pid, _ids = confirmed_project(client)
    assert client.post(plans_url(pid)).status_code == 409  # nothing at all
    serve(alias_payload(_ids))
    assert generate(client, pid).status_code == 201  # a draft is not enough
    response = client.post(plans_url(pid))
    assert response.status_code == 409
    assert "approved" in response.json()["detail"]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ImplementationPlan)) == 0


def test_no_plan_when_the_graph_is_not_built(client: TestClient, engine: Engine) -> None:
    pid, _proposal_id, _ = approved_project(client)
    with Session(engine) as session:
        session.execute(delete(GraphEdge))
        session.execute(delete(GraphNode))
        session.commit()
    response = client.post(plans_url(pid))
    assert response.status_code == 409
    assert "graph" in response.json()["detail"]


def test_cycle_fails_the_build_and_stores_nothing(client: TestClient, engine: Engine) -> None:
    def cycle(p: dict[str, Any]) -> None:
        p["diagram"]["edges"].append({"source": "db", "target": "web", "relationship": "oops"})

    pid, _proposal_id, _ = approved_project(client, cycle)
    response = client.post(plans_url(pid))
    assert response.status_code == 422
    assert "cycle" in response.json()["detail"]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ImplementationPlan)) == 0


def test_rebuild_creates_a_new_version_and_leaves_an_approved_plan_alone(
    client: TestClient,
) -> None:
    pid, _proposal_id, _ = approved_project(client)
    first = build(client, pid)
    approved = client.post(plans_url(pid, f"/{first['id']}/approve"))
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    second = build(client, pid)
    assert (second["version"], second["status"]) == (2, "draft")
    assert second["id"] != first["id"]
    assert client.get(plans_url(pid, "/" + first["id"])).json() == approved.json()
    assert [p["version"] for p in client.get(plans_url(pid)).json()] == [2, 1]


def test_approve_and_reject_are_for_drafts_only(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    a, b = build(client, pid), build(client, pid)
    assert client.post(plans_url(pid, f"/{a['id']}/approve")).status_code == 200
    for action in ("approve", "reject"):
        assert client.post(plans_url(pid, f"/{a['id']}/{action}")).status_code == 409
    assert client.post(plans_url(pid, f"/{b['id']}/reject")).json()["status"] == "rejected"
    assert client.post(plans_url(pid, f"/{b['id']}/approve")).status_code == 409


def test_plans_are_isolated_between_projects(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    plan = build(client, pid)
    other = make_project(client, "Other")
    assert client.get(plans_url(other)).json() == []
    assert client.get(plans_url(other, "/" + plan["id"])).status_code == 404
    assert client.post(plans_url(other, f"/{plan['id']}/approve")).status_code == 404
    assert client.post(plans_url(other)).status_code == 409  # its own architecture is missing
    unknown = str(uuid.uuid4())
    assert client.post(plans_url(unknown)).status_code == 404
    assert client.get(plans_url(unknown)).status_code == 404
    assert client.get(plans_url(pid, "/" + plan["id"])).status_code == 200


# ---- MCP -------------------------------------------------------------------------------------


@pytest.fixture
def mcp_world(client: TestClient, engine: Engine) -> tuple[ToolContext, str, dict[str, Any]]:
    pid, _proposal_id, _ = approved_project(client)
    plan = build(client, pid)
    return ToolContext(sessionmaker(bind=engine, expire_on_commit=False)), pid, plan


def test_get_plan_defaults_to_the_latest_approved_plan(
    client: TestClient, mcp_world: tuple[ToolContext, str, dict[str, Any]]
) -> None:
    ctx, pid, plan = mcp_world
    with pytest.raises(ToolInputError, match="no approved implementation plan"):
        tools.get_plan(ctx, pid)
    client.post(plans_url(pid, f"/{plan['id']}/approve"))
    build(client, pid)  # a newer draft must not win
    index = tools.get_plan(ctx, pid)
    assert (index["version"], index["status"], index["plan_id"]) == (1, "approved", plan["id"])
    assert index["sections_index"] == {
        "tasks": {"items": 7},
        "unattached_questions": {"items": 1},
    }
    assert "content" not in index
    assert tools.get_plan(ctx, pid, version=2)["status"] == "draft"
    with pytest.raises(ToolInputError, match="version 9"):
        tools.get_plan(ctx, pid, version=9)


def test_get_plan_sections_and_truncation(
    mcp_world: tuple[ToolContext, str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, pid, plan = mcp_world
    full = tools.get_plan(ctx, pid, version=1, sections=["tasks"])
    assert [t["key"] for t in full["content"]["tasks"]] == [t["key"] for t in plan["tasks"]]
    assert full["content"]["tasks"][0]["depends_on"] == []
    assert full["truncated"] is False and full["truncated_sections"] == {}
    monkeypatch.setattr(tools, "MAX_SECTION_ITEMS", 3)
    cut = tools.get_plan(ctx, pid, version=1, sections=["tasks"])
    assert len(cut["content"]["tasks"]) == 3
    assert cut["truncated_sections"] == {"tasks": {"returned": 3, "total": 7}}
    assert cut["truncated"] is True
    with pytest.raises(ToolInputError, match="Unknown section"):
        tools.get_plan(ctx, pid, version=1, sections=["nope"])


def test_get_plan_is_scoped_to_its_project(
    client: TestClient, mcp_world: tuple[ToolContext, str, dict[str, Any]]
) -> None:
    ctx, pid, plan = mcp_world
    client.post(plans_url(pid, f"/{plan['id']}/approve"))
    other = make_project(client, "Other")
    with pytest.raises(ToolInputError, match="no approved implementation plan"):
        tools.get_plan(ctx, other)
    with pytest.raises(ToolInputError, match="Unknown project_id"):
        tools.get_plan(ctx, str(uuid.uuid4()))
