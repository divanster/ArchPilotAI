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
    NO_DEPENDENCIES_WARNING,
    UNMAPPED_PREFIX,
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

    def atlas_reversed(p: dict[str, Any]) -> None:
        atlas(p)
        p["diagram"]["edges"].reverse()

    assert plan_of(atlas) == plan_of(atlas_reversed)


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
    assert all(t.depends_on == [] for t in spec.tasks if t.kind != "nfr")
    assert spec.dependency_count == 0  # counts diagram dependencies only
    assert NO_DEPENDENCIES_WARNING in spec.warnings


def atlas(p: dict[str, Any]) -> None:
    """Diagram ids (n-*) and component ids (c-*) differ; only the names agree."""
    ren = {"web": "n-web", "api": "n-api", "db": "n-db", "idp": "n-idp"}
    for node in p["diagram"]["nodes"]:
        node["id"] = ren[node["id"]]
    for edge in p["diagram"]["edges"]:
        edge["source"], edge["target"] = ren[edge["source"]], ren[edge["target"]]
    for comp in p["components"]:
        comp["id"] = "c-" + comp["id"]
    for entry in p["coverage_matrix"]:
        entry["related_ids"] = ["c-" + r if r in ren else r for r in entry["related_ids"]]


def test_diagram_nodes_map_to_components_by_exact_name_not_id() -> None:
    spec = plan_of(atlas)
    keys = {t.title: t.key for t in spec.tasks}
    assert by_title(spec, "Implement Web UI").depends_on == [keys["Implement API"]]
    assert by_title(spec, "Implement API").depends_on == sorted(
        [keys["Implement Database"], keys["Implement Identity provider"]]
    )
    assert spec.dependency_count == 3
    assert spec.unmapped_nodes == []
    assert not spec.warnings
    # database/identity before backend before frontend
    assert titles(spec)[:4] == [
        "Implement Database",
        "Implement Identity provider",
        "Implement API",
        "Implement Web UI",
    ]


def test_unmapped_node_is_reported_and_never_guessed_or_ordered() -> None:
    def entra(p: dict[str, Any]) -> None:
        atlas(p)
        for node in p["diagram"]["nodes"]:
            if node["id"] == "n-idp":
                node["name"] = "Entra ID"  # no component has this exact name

    spec = plan_of(entra)
    api = by_title(spec, "Implement API")
    identity = by_title(spec, "Implement Identity provider")
    assert spec.unmapped_nodes == ["Entra ID"]
    assert spec.dependency_count == 2
    assert identity.key not in api.depends_on
    assert any(n.startswith(UNMAPPED_PREFIX) and "Entra ID" in n for n in api.notes)
    assert identity.notes == []
    assert any("Entra ID" in w for w in spec.warnings)
    assert titles(spec).index("Implement API") < titles(spec).index("Implement Identity provider")


def test_names_are_matched_exactly_not_by_case_or_similarity() -> None:
    def near(p: dict[str, Any]) -> None:
        atlas(p)
        for node in p["diagram"]["nodes"]:
            if node["id"] == "n-db":
                node["name"] = "database"

    spec = plan_of(near)
    assert by_title(spec, "Implement API").depends_on == [
        by_title(spec, "Implement Identity provider").key
    ]
    assert spec.unmapped_nodes == ["database"]


def test_duplicate_component_names_are_ambiguous_and_unmapped() -> None:
    def dup(p: dict[str, Any]) -> None:
        atlas(p)
        p["components"][2]["name"] = "Web UI"  # now two components share a name

    spec = plan_of(dup)
    assert "Web UI" in spec.unmapped_nodes
    assert by_title(spec, "Implement API").depends_on != []
    web = by_title(spec, "Implement Web UI").key
    assert all(web not in t.depends_on for t in spec.tasks if t.kind != "nfr")


def test_related_hint_links_decision_tasks_but_never_orders() -> None:
    spec = plan_of()
    secure = by_title(spec, "Secure: Access control")
    components = {t.key for t in spec.tasks if t.kind in ("component", "data")}
    assert secure.related and set(secure.related) <= components
    assert by_title(spec, "Implement API").related == []
    assert secure.depends_on == []


def test_meta_open_questions_go_to_not_for_client() -> None:
    def questions(p: dict[str, Any]) -> None:
        p["open_questions"] = [
            {
                "question": "Why were no passages cited in this proposal?",
                "requirement_ids": [FAKE_IDS[0]],
            },
            {"question": "Which IdP is used?", "requirement_ids": [FAKE_IDS[0]]},
        ]

    spec = plan_of(questions)
    assert spec.not_for_client == ["Why were no passages cited in this proposal?"]
    assert spec.unattached_questions == []
    blocking = [q for t in spec.tasks for q in t.blocking_questions]
    assert blocking and set(blocking) == {"Which IdP is used?"}


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
    assert (plan["dependency_count"], plan["unmapped_node_count"], plan["warnings"]) == (3, 0, [])
    assert plan["not_for_client"] == []
    assert any(t["depends_on"] for t in plan["tasks"])
    assert any(t["related"] for t in plan["tasks"])
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


def test_nfr_rule_orders_nfr_after_related_components_and_labels_the_source() -> None:
    spec = plan_of(atlas)
    nfr = by_title(spec, "NFR: Performance")
    assert nfr.related and nfr.depends_on == nfr.related
    assert set(nfr.dependency_sources.values()) == {"nfr rule"}
    position = {t.key: t.order_index for t in spec.tasks}
    assert all(position[d] < nfr.order_index for d in nfr.depends_on)
    api = by_title(spec, "Implement API")
    assert set(api.dependency_sources.values()) == {"diagram"}
    # The related hint of a security task stays a hint and orders nothing.
    assert by_title(spec, "Secure: Access control").dependency_sources == {}
    assert spec.dependency_count == 3  # diagram dependencies only


def test_questions_attach_only_through_a_cited_shared_requirement_atlas() -> None:
    def questions(p: dict[str, Any]) -> None:
        atlas(p)
        p["open_questions"] = [
            {"question": "Retention?", "requirement_ids": [FAKE_IDS[1]]},
            {"question": "Unknown requirement?", "requirement_ids": [str(uuid.UUID(int=999))]},
            {"question": "Who owns operations?", "requirement_ids": []},
        ]

    spec = plan_of(questions)
    cited = {t.title for t in spec.tasks if "Retention?" in t.blocking_questions}
    assert cited == {t.title for t in spec.tasks if FAKE_IDS[1] in map(str, t.requirement_ids)}
    assert cited and "Secure: Access control" not in cited
    assert all(
        "Unknown requirement?" not in t.blocking_questions
        and "Who owns operations?" not in t.blocking_questions
        for t in spec.tasks
    )
    assert spec.unattached_questions == ["Unknown requirement?", "Who owns operations?"]


# ---- manual dependency edits -----------------------------------------------------------------


def task_ids(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["title"]: t for t in plan["tasks"]}


def edit(
    client: TestClient,
    pid: str,
    plan: dict[str, Any],
    task: str,
    target: str,
    action: str = "dependencies",
    reason: str = "Needs the interface first",
    author: str = "ivan",
) -> Any:
    tasks = task_ids(plan)
    return client.post(
        plans_url(pid, f"/{plan['id']}/{action}"),
        json={
            "task_id": tasks[task]["id"],
            "depends_on_task_id": tasks[target]["id"],
            "reason": reason,
            "author": author,
        },
    )


def test_manual_add_and_remove_with_audit_fields(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    plan = build(client, pid)
    before = task_ids(plan)
    assert before["NFR: Performance"]["dependencies"]
    assert {d["source"] for d in before["NFR: Performance"]["dependencies"]} == {"nfr rule"}
    assert {d["source"] for d in before["Implement API"]["dependencies"]} == {"diagram"}

    response = edit(client, pid, plan, "Secure: Access control", "Implement Web UI")
    assert response.status_code == 200, response.text
    body = response.json()
    secure = task_ids(body)["Secure: Access control"]
    web_key = before["Implement Web UI"]["key"]
    assert web_key in secure["depends_on"]
    dep = next(d for d in secure["dependencies"] if d["key"] == web_key)
    assert (dep["source"], dep["reason"], dep["author"]) == (
        "manual",
        "Needs the interface first",
        "ivan",
    )
    [recorded] = body["manual_edits"]
    assert recorded["action"] == "add"
    assert (recorded["task_key"], recorded["depends_on_key"]) == (secure["key"], web_key)
    assert recorded["author"] == "ivan" and recorded["created_at"]
    fetched = client.get(plans_url(pid, "/" + plan["id"])).json()["manual_edits"]
    assert [e["reason"] for e in fetched] == [e["reason"] for e in body["manual_edits"]]

    duplicate = edit(client, pid, plan, "Secure: Access control", "Implement Web UI")
    assert duplicate.status_code == 422 and "already depends" in duplicate.json()["detail"]

    removed = edit(
        client,
        pid,
        plan,
        "Secure: Access control",
        "Implement Web UI",
        "dependencies/remove",
        reason="Not needed",
    )
    assert removed.status_code == 200, removed.text
    assert web_key not in task_ids(removed.json())["Secure: Access control"]["depends_on"]
    assert [e["action"] for e in removed.json()["manual_edits"]] == ["add", "remove"]
    again = edit(
        client, pid, plan, "Secure: Access control", "Implement Web UI", "dependencies/remove"
    )
    assert again.status_code == 422


def test_manual_edit_reorders_so_dependencies_come_first(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    plan = build(client, pid)
    response = edit(client, pid, plan, "Implement Identity provider", "Integrate: Identity")
    assert response.status_code == 200, response.text
    out = response.json()
    position = {t["key"]: t["order_index"] for t in out["tasks"]}
    assert sorted(position.values()) == list(range(1, len(position) + 1))
    for t in out["tasks"]:
        assert all(position[d] < t["order_index"] for d in t["depends_on"])


def test_manual_edit_rejects_cycles_self_and_other_plans(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    plan = build(client, pid)
    other_plan = build(client, pid)
    cycle = edit(client, pid, plan, "Implement Database", "Implement Web UI")
    assert cycle.status_code == 422 and "cycle" in cycle.json()["detail"]
    own = edit(client, pid, plan, "Implement API", "Implement API")
    assert own.status_code == 422 and "itself" in own.json()["detail"]
    mixed = client.post(
        plans_url(pid, f"/{plan['id']}/dependencies"),
        json={
            "task_id": task_ids(plan)["Implement API"]["id"],
            "depends_on_task_id": task_ids(other_plan)["Implement Web UI"]["id"],
            "reason": "x",
            "author": "ivan",
        },
    )
    assert mixed.status_code == 422 and "this plan" in mixed.json()["detail"]
    for field, value in (("reason", "  "), ("author", "")):
        payload = {
            "task_id": task_ids(plan)["Secure: Access control"]["id"],
            "depends_on_task_id": task_ids(plan)["Implement API"]["id"],
            "reason": "why",
            "author": "ivan",
            field: value,
        }
        bad = client.post(plans_url(pid, f"/{plan['id']}/dependencies"), json=payload)
        assert bad.status_code == 422
    unchanged = client.get(plans_url(pid, "/" + plan["id"])).json()
    assert unchanged["manual_edits"] == []
    assert task_ids(unchanged)["Implement Database"]["depends_on"] == []


def test_approved_and_rejected_plans_are_read_only(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    a, b = build(client, pid), build(client, pid)
    client.post(plans_url(pid, f"/{a['id']}/approve"))
    client.post(plans_url(pid, f"/{b['id']}/reject"))
    for plan in (a, b):
        for action in ("dependencies", "dependencies/remove"):
            response = edit(client, pid, plan, "Secure: Access control", "Implement API", action)
            assert response.status_code == 409
    approved = client.get(plans_url(pid, "/" + a["id"])).json()
    assert approved["manual_edits"] == []


def test_edits_are_scoped_to_the_project(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    plan = build(client, pid)
    other = make_project(client, "Other")
    response = edit(client, other, plan, "Secure: Access control", "Implement API")
    assert response.status_code == 404


def test_rebuild_does_not_carry_manual_edits_but_lists_them(client: TestClient) -> None:
    pid, _proposal_id, _ = approved_project(client)
    first = build(client, pid)
    assert first["previous_version"] is None and first["previous_manual_edits"] == []
    assert edit(client, pid, first, "Secure: Access control", "Implement Web UI").status_code == 200
    second = build(client, pid)
    assert second["manual_edits"] == []
    assert all(d["source"] != "manual" for t in second["tasks"] for d in t["dependencies"])
    assert second["previous_version"] == 1
    [note] = second["previous_manual_edits"]
    assert (note["action"], note["author"], note["reason"]) == (
        "add",
        "ivan",
        "Needs the interface first",
    )


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
        "manual_edits": {"items": 0},
        "unattached_questions": {"items": 1},
        "not_for_client": {"items": 0},
    }
    assert "content" not in index
    assert tools.get_plan(ctx, pid, version=2)["status"] == "draft"
    with pytest.raises(ToolInputError, match="version 9"):
        tools.get_plan(ctx, pid, version=9)


def test_get_plan_returns_manual_edits_and_dependency_sources(
    client: TestClient, mcp_world: tuple[ToolContext, str, dict[str, Any]]
) -> None:
    ctx, pid, plan = mcp_world
    assert edit(client, pid, plan, "Secure: Access control", "Implement Web UI").status_code == 200
    result = tools.get_plan(ctx, pid, version=1, sections=["tasks", "manual_edits"])
    tasks = {t["title"]: t for t in result["content"]["tasks"]}
    sources = {d["source"] for t in tasks.values() for d in t["dependencies"]}
    assert sources == {"diagram", "nfr rule", "manual"}
    manual = [d for d in tasks["Secure: Access control"]["dependencies"] if d["source"] == "manual"]
    assert manual[0]["reason"] == "Needs the interface first"
    [recorded] = result["content"]["manual_edits"]
    assert recorded["author"] == "ivan" and recorded["action"] == "add"
    assert result["previous_version"] is None


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
