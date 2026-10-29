import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.domain.decisions import BLOCK_LABEL, area_for_task
from app.domain.plan import AI_RECOMMENDATION_FLAG
from app.mcp import tools
from app.mcp.tools import ToolContext
from app.models.decision import ProjectDecision
from tests.test_agent import (
    clean_agent_state as _clean_agent_state,
)
from tests.test_agent import (
    context_of,
    finish,
    run_of,
    scripted,
    start,
    write,
)
from tests.test_patches import (
    World,
    api,
    create,
    generate,
    serve,
    set_task,
)
from tests.test_patches import (
    mcp_ctx as _mcp_ctx,
)
from tests.test_patches import (
    world as _world,
)
from tests.test_requirements_api import make_project

world = _world
mcp_ctx = _mcp_ctx
clean_agent_state = _clean_agent_state

TITLE = "Implement backend API"


def decide(
    client: TestClient, pid: str, area: str = "backend", choice: str = "FastAPI", **extra: Any
) -> Any:
    body = {"area": area, "choice": choice, "reason": "team skills", "name": "Ana", **extra}
    return client.post(api(pid, "/decisions"), json=body)


def flag_first_task(engine: Engine, world: World, title: str = TITLE) -> str:
    _pid, _plan, tasks = world
    task = next(iter(tasks.values()))
    set_task(engine, task["id"], flags=[AI_RECOMMENDATION_FLAG], title=title, kind="component")
    return str(task["id"])


def test_create_and_supersede_never_edits_in_place(client: TestClient, world: World) -> None:
    pid = world[0]
    first = decide(client, pid)
    assert first.status_code == 201, first.text
    second = decide(client, pid, choice="Django")
    assert second.status_code == 201
    listing = client.get(api(pid, "/decisions")).json()
    assert [(d["area"], d["choice"]) for d in listing["current"]] == [("backend", "Django")]
    assert len(listing["history"]) == 1
    old = listing["history"][0]
    assert old["choice"] == "FastAPI" and old["superseded_by"] == second.json()["id"]
    assert old["reason"] == "team skills" and old["decided_by"] == "Ana"
    assert listing["current"][0]["superseded_by"] is None


def test_validation(client: TestClient, world: World) -> None:
    pid = world[0]
    assert decide(client, pid, area="cooking").status_code == 422
    assert decide(client, pid, choice="").status_code == 422
    assert decide(client, pid, choice="   ").status_code == 422
    assert decide(client, pid, reason="").status_code == 422
    assert decide(client, pid, name="").status_code == 422
    assert decide(client, pid, name=" ").status_code == 422
    assert decide(client, str(uuid.uuid4())).status_code == 404
    assert client.get(api(pid, "/decisions")).json()["current"] == []


def test_suggestions_come_from_the_approved_architecture(client: TestClient, world: World) -> None:
    body = client.get(api(world[0], "/decisions")).json()
    assert body["suggestions"], "the approved architecture has a recommended stack"
    for suggestion in body["suggestions"].values():
        assert suggestion["label"] == "AI recommendation" and suggestion["choice"]


def test_only_current_decisions_are_injected_into_generation(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    decide(client, pid, choice="Flask")
    decide(client, pid, choice="Django")
    decide(client, pid, area="database", choice="PostgreSQL")
    provider = serve([create("src/a.py")])
    assert generate(client, world).status_code == 201
    block = context_of(provider.requests[-1].prompt)[BLOCK_LABEL]
    assert {(d["area"], d["choice"]) for d in block} == {
        ("backend", "Django"),
        ("database", "PostgreSQL"),
    }
    assert "Flask" not in provider.requests[-1].prompt


def test_agent_runs_receive_current_decisions(client: TestClient, world: World) -> None:
    pid = world[0]
    decide(client, pid, choice="Flask")
    decide(client, pid, choice="Django")
    provider = scripted([write("src/g.py"), finish()])
    run_of(client, world, start(client, world))
    for request in provider.requests:
        block = context_of(request.prompt)[BLOCK_LABEL]
        assert [(d["area"], d["choice"]) for d in block] == [("backend", "Django")]
        assert "Flask" not in request.prompt


def test_no_decisions_gives_an_empty_labelled_block(client: TestClient, world: World) -> None:
    provider = serve([create("src/a.py")])
    assert generate(client, world).status_code == 201
    assert context_of(provider.requests[-1].prompt)[BLOCK_LABEL] == []


def test_a_decision_releases_the_technology_gate(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    flag_first_task(engine, world)
    serve([create("src/a.py")])
    blocked = generate(client, world)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "technology_unconfirmed"
    assert decide(client, pid).status_code == 201
    provider = serve([create("src/a.py")])
    assert generate(client, world).status_code == 201
    task = context_of(provider.requests[-1].prompt)["task"]
    assert AI_RECOMMENDATION_FLAG not in task["flags"]
    assert "Confirmed by Ana: FastAPI" in task["flags"]


def test_a_decision_releases_the_agent_gate(
    client: TestClient, engine: Engine, world: World
) -> None:
    flag_first_task(engine, world)
    scripted([write("src/g.py"), finish()])
    assert start(client, world).status_code == 409
    decide(client, world[0])
    provider = scripted([write("src/g.py"), finish()])
    run_of(client, world, start(client, world))
    task = context_of(provider.requests[0].prompt)["task"]
    assert "Confirmed by Ana: FastAPI" in task["flags"]


def test_a_decision_for_another_area_keeps_the_gate(
    client: TestClient, engine: Engine, world: World
) -> None:
    flag_first_task(engine, world)
    decide(client, world[0], area="hosting", choice="Azure")
    serve([create("src/a.py")])
    blocked = generate(client, world)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "technology_unconfirmed"


def test_no_decision_keeps_the_gate_with_a_clear_reason(
    client: TestClient, engine: Engine, world: World
) -> None:
    flag_first_task(engine, world)
    serve([create("src/a.py")])
    blocked = generate(client, world)
    assert blocked.status_code == 409
    assert "decision" in blocked.json()["detail"]["message"]


def test_deviation_from_the_ai_recommendation_is_shown(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    suggestions = client.get(api(pid, "/decisions")).json()["suggestions"]
    area, suggestion = next(iter(suggestions.items()))
    task = next(iter(world[2].values()))
    set_task(
        engine,
        task["id"],
        flags=[AI_RECOMMENDATION_FLAG],
        title=f"Implement {suggestion['layer']}",
        kind="component",
    )
    assert area_for_task("component", f"Implement {suggestion['layer']}") == area
    decide(client, pid, area=area, choice="Something Else Entirely")
    provider = serve([create("src/a.py")])
    assert generate(client, world).status_code == 201
    flags = context_of(provider.requests[-1].prompt)["task"]["flags"]
    assert "Confirmed by Ana: Something Else Entirely" in flags
    assert (
        f"Deviation: AI recommended {suggestion['choice']}, "
        "the human decision is Something Else Entirely" in flags
    )
    # Choosing the recommendation itself shows no deviation.
    decide(client, pid, area=area, choice=suggestion["choice"].upper())
    provider = serve([create("src/b.py")])
    assert generate(client, world).status_code == 201
    flags = context_of(provider.requests[-1].prompt)["task"]["flags"]
    assert not any(f.startswith("Deviation") for f in flags)


def test_projects_are_isolated(client: TestClient, engine: Engine, world: World) -> None:
    pid = world[0]
    other = make_project(client)
    decide(client, pid)
    assert client.get(api(other, "/decisions")).json()["current"] == []
    assert decide(client, other, choice="Django").status_code == 201
    assert [d["choice"] for d in client.get(api(pid, "/decisions")).json()["current"]] == [
        "FastAPI"
    ]
    flag_first_task(engine, world)
    provider = serve([create("src/a.py")])
    assert generate(client, world).status_code == 201
    assert "Django" not in provider.requests[-1].prompt
    with Session(engine) as session:
        rows = session.scalars(select(ProjectDecision)).all()
    assert {r.project_id for r in rows} == {uuid.UUID(pid), uuid.UUID(other)}


def test_mcp_get_plan_includes_current_decisions(
    client: TestClient, world: World, mcp_ctx: ToolContext
) -> None:
    pid = world[0]
    decide(client, pid, choice="Flask")
    decide(client, pid, choice="Django")
    decisions = tools.get_plan(mcp_ctx, pid)["decisions"]
    assert decisions["label"] == BLOCK_LABEL
    assert [(d["area"], d["choice"]) for d in decisions["items"]] == [("backend", "Django")]


def test_only_one_current_row_per_area_is_enforced(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    decide(client, pid)
    with Session(engine) as session:
        session.add(
            ProjectDecision(
                project_id=uuid.UUID(pid), area="backend", choice="x", reason="r", decided_by="n"
            )
        )
        with pytest.raises(Exception, match="UNIQUE|unique"):
            session.commit()
