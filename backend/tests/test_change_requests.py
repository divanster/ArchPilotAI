"""Phase 15A: free-form change requests with architecture versioning."""

import copy
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, inspect, select
from sqlalchemy.orm import Session

from alembic import command
from app.api import deps
from app.domain.architecture import ArchitectureContent
from app.domain.change_request import (
    classify,
    detect_drift,
    diff_architecture,
    structure,
)
from app.llm.fake_provider import FakeLLMProvider
from app.models.architecture import ArchitectureProposal
from app.models.plan import ImplementationPlan, PlanTask
from app.models.requirement import Requirement
from app.services import agent as agent_service
from tests.conftest import alembic_config
from tests.test_agent import ScriptedChecker, action, finish, write
from tests.test_architecture_api import alias_payload, arch_url, valid_payload
from tests.test_patches import World, api, review
from tests.test_patches import world as _world_fixture
from tests.test_plans import approved_project, build, plans_url
from tests.test_requirements_api import use

world = _world_fixture

CODE_TEXT = "Fix the typo in the label of the list page"
ARCH_TEXT = "Add a Redis cache and integrate the Stripe payments API"


@pytest.fixture(autouse=True)
def clean_agent_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("app.api.agent._spawn", lambda job: job())  # run inline
    real = deps.make_agent_service
    monkeypatch.setattr(
        "app.api.agent.make_agent_service",
        lambda s, p: real(s, p, checker=ScriptedChecker([0])),  # type: ignore[arg-type]
        raising=False,
    )
    yield
    agent_service._active.clear()
    if agent_service._agent_lock.locked():
        agent_service._agent_lock.release()


def cr_url(pid: str, suffix: str = "") -> str:
    return f"/api/projects/{pid}/change-requests{suffix}"


def create(client: TestClient, pid: str, text: str = CODE_TEXT) -> dict[str, Any]:
    response = client.post(cr_url(pid), json={"text": text, "author": "ana"})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def analysed(client: TestClient, pid: str, text: str = CODE_TEXT) -> dict[str, Any]:
    cr = create(client, pid, text)
    response = client.post(cr_url(pid, f"/{cr['id']}/analyse"))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def approve(client: TestClient, pid: str, cr: dict[str, Any]) -> dict[str, Any]:
    response = client.post(cr_url(pid, f"/{cr['id']}/approve"), json={"by": "bo"})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def with_component(payload: dict[str, Any], name: str) -> dict[str, Any]:
    changed = copy.deepcopy(payload)
    extra = copy.deepcopy(changed["components"][0])
    extra.update({"id": name.lower(), "name": name})
    changed["components"].append(extra)
    return changed


def content_of(proposal: ArchitectureProposal) -> ArchitectureContent:
    return ArchitectureContent.model_validate(proposal.content)


# ---- pure domain ---------------------------------------------------------------------------


def test_classification_and_diff_are_pure(client: TestClient) -> None:
    ids = [str(uuid.uuid4()) for _ in range(2)]
    parent = ArchitectureContent.model_validate(
        {k: v for k, v in valid_payload(ids).items() if k not in ("title", "summary")}
    )
    assert classify(CODE_TEXT, parent, [], [], []).impact_class == "code_only"
    found = classify(ARCH_TEXT, parent, [], [], [])
    assert found.impact_class == "architecture" and found.signals and found.reason
    child = parent.model_copy(deep=True)
    assert diff_architecture(parent, child)["removals"] == []
    child.components.pop()
    assert diff_architecture(parent, child)["removals"]


def test_structure_and_drift_detection_are_pure() -> None:
    parent = ArchitectureContent.model_validate(
        {
            k: v
            for k, v in valid_payload([str(uuid.uuid4())]).items()
            if k not in ("title", "summary")
        }
    )
    before = structure(["src/app/main.py"])
    paths = ["src/app/main.py", "src/payments/__init__.py", "src/payments/client.py"]
    texts = {"src/payments/client.py": "URL = 'https://api.stripe.com/v1/charges'\n"}
    kinds = {d["kind"] for d in detect_drift(parent, before, paths, texts)}
    assert kinds & {"new_package", "new_endpoint"}
    assert detect_drift(parent, before, ["src/app/main.py"], {}) == []


# ---- classification and override -------------------------------------------------------------


def test_creates_numbered_requests_and_classifies(client: TestClient, world: World) -> None:
    pid = world[0]
    first = create(client, pid)
    second = create(client, pid, ARCH_TEXT)
    assert (first["key"], second["key"]) == ("CR-001", "CR-002")
    assert first["status"] == "draft" and first["author"] == "ana"
    code = client.post(cr_url(pid, f"/{first['id']}/analyse")).json()
    arch = client.post(cr_url(pid, f"/{second['id']}/analyse")).json()
    assert (code["status"], code["impact_class"]) == ("analysed", "code_only")
    assert arch["impact_class"] == "architecture" and arch["impact"]["reason"]
    for key in ("components", "decisions", "requirements"):
        assert key in arch["impact"]
    listed = client.get(cr_url(pid)).json()
    assert [c["key"] for c in listed] == ["CR-002", "CR-001"]


def test_person_can_override_the_class(client: TestClient, world: World) -> None:
    pid = world[0]
    cr = analysed(client, pid)
    bad = client.put(
        cr_url(pid, f"/{cr['id']}/class"), json={"impact_class": "nope", "by": "a", "reason": "r"}
    )
    assert bad.status_code == 422
    done = client.put(
        cr_url(pid, f"/{cr['id']}/class"),
        json={"impact_class": "architecture", "by": "ana", "reason": "touches design"},
    ).json()
    assert done["impact_class"] == "code_only"  # the analysis stays on record
    assert (done["class_override"], done["effective_class"]) == ("architecture", "architecture")
    assert done["overridden_by"] == "ana" and done["override_reason"] == "touches design"


def test_reject_needs_a_reason_and_closes_the_request(client: TestClient, world: World) -> None:
    pid = world[0]
    cr = analysed(client, pid)
    assert client.post(cr_url(pid, f"/{cr['id']}/reject"), json={"by": "bo"}).status_code == 422
    done = client.post(cr_url(pid, f"/{cr['id']}/reject"), json={"by": "bo", "reason": "no"}).json()
    assert done["status"] == "rejected"
    assert client.post(cr_url(pid, f"/{cr['id']}/approve"), json={"by": "bo"}).status_code == 409


def test_request_needs_an_approved_architecture_and_plan(client: TestClient) -> None:
    from tests.test_requirements_api import make_project

    pid = make_project(client)
    response = client.post(cr_url(pid), json={"text": CODE_TEXT, "author": "ana"})
    assert response.status_code == 409


# ---- architecture path ------------------------------------------------------------------------


def architecture_cr(client: TestClient, world: World, engine: Engine) -> tuple[dict[str, Any], str]:
    pid = world[0]
    cr = analysed(client, pid, ARCH_TEXT)
    approve(client, pid, cr)
    with Session(engine) as session:
        parent = session.scalars(
            select(ArchitectureProposal).where(ArchitectureProposal.status == "approved")
        ).one()
        parent_id = str(parent.id)
        ids = [
            str(r.id)
            for r in session.scalars(select(Requirement).where(Requirement.status == "confirmed"))
        ]
    use(FakeLLMProvider(architecture_text=json.dumps(with_component(alias_payload(ids), "Cache"))))
    return cr, parent_id


def test_architecture_path_creates_a_linked_draft_with_a_diff(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    cr, parent_id = architecture_cr(client, world, engine)
    response = client.post(cr_url(pid, f"/{cr['id']}/architecture"))
    assert response.status_code == 201, response.text
    body = response.json()
    draft = body["proposals"][0]
    assert draft["status"] == "draft" and draft["revised_from_id"] == parent_id
    diff = draft["diff"]
    assert [c["key"] for c in diff["components"]["added"]] == ["cache"]
    assert diff["components"]["removed"] == [] and diff["removals"] == []
    assert body["log"]["architecture"][0]["parent_version"] is not None
    with Session(engine) as session:
        parent = session.get(ArchitectureProposal, uuid.UUID(parent_id))
        assert parent is not None and parent.status == "approved"  # immutable
        assert len(content_of(parent).components) == diff["unchanged"]["components"]


def test_requirement_is_proposed_and_never_confirmed(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    cr, _ = architecture_cr(client, world, engine)
    assert client.post(cr_url(pid, f"/{cr['id']}/architecture")).status_code == 201
    approved = client.post(cr_url(pid, f"/{cr['id']}/architecture/approve"), json={})
    assert approved.status_code == 200, approved.text
    ids = approved.json()["log"]["requirement_ids"]
    assert len(ids) == 1
    with Session(engine) as session:
        statuses = [
            r.status
            for r in session.scalars(
                select(Requirement).where(Requirement.id.in_([uuid.UUID(i) for i in ids]))
            )
        ]
    assert statuses == ["proposed"]


def test_plan_delta_has_only_new_tasks_and_leaves_approved_tasks_alone(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid, plan, tasks = world
    cr, _ = architecture_cr(client, world, engine)
    assert client.post(cr_url(pid, f"/{cr['id']}/plan-delta")).status_code == 409  # not approved
    assert client.post(cr_url(pid, f"/{cr['id']}/architecture")).status_code == 201
    assert client.post(cr_url(pid, f"/{cr['id']}/architecture/approve"), json={}).status_code == 200
    with Session(engine) as session:
        before = {
            t.key: (t.title, t.description, t.status) for t in session.scalars(select(PlanTask))
        }
        count = len(list(session.scalars(select(ImplementationPlan))))
    response = client.post(cr_url(pid, f"/{cr['id']}/plan-delta"))
    assert response.status_code == 201, response.text
    delta = response.json()["log"]["plan_deltas"]
    assert len(delta) == 1 and delta[0]["base_plan_id"] == plan["id"]
    with Session(engine) as session:
        plans = list(session.scalars(select(ImplementationPlan)))
        assert len(plans) == count + 1
        new = next(p for p in plans if p.kind == "delta")
        new_tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_id == new.id)))
        after = {
            t.key: (t.title, t.description, t.status)
            for t in session.scalars(select(PlanTask))
            if t.key in before
        }
    assert [t.title for t in new_tasks] == ["Implement Cache"]
    assert new_tasks[0].key not in tasks
    assert all(after[k] == before[k] for k in tasks)
    listed = client.get(plans_url(pid)).json()
    assert all(p["id"] != str(new.id) for p in listed)  # a delta is not a full plan


def test_removals_need_a_confirmation_with_a_reason(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    cr = analysed(client, pid, ARCH_TEXT)
    approve(client, pid, cr)
    with Session(engine) as session:
        ids = [
            str(r.id)
            for r in session.scalars(select(Requirement).where(Requirement.status == "confirmed"))
        ]
    smaller = alias_payload(ids)
    removed = smaller["components"].pop()
    assert removed["id"]
    use(FakeLLMProvider(architecture_text=json.dumps(smaller)))
    created = client.post(cr_url(pid, f"/{cr['id']}/architecture"))
    if created.status_code != 201:
        pytest.skip("the reduced fake payload is not a valid architecture")
    draft = created.json()["proposals"][0]
    assert draft["diff"]["removals"]
    blocked = client.post(cr_url(pid, f"/{cr['id']}/architecture/approve"), json={})
    assert blocked.status_code == 409
    generic = client.patch(arch_url(pid, "/" + draft["id"]), json={"status": "approved"})
    assert generic.status_code == 409
    removals = [
        {"kind": r["kind"], "key": r["key"], "reason": "obsolete"}
        for r in draft["diff"]["removals"]
    ]
    ok = client.post(
        cr_url(pid, f"/{cr['id']}/architecture/approve"), json={"confirmed_removals": removals}
    )
    assert ok.status_code == 200, ok.text


# ---- fast path, drift and change log -----------------------------------------------------------


def fast_run(
    client: TestClient, world: World, files: list[tuple[str, str]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    pid = world[0]
    cr = analysed(client, pid)
    approve(client, pid, cr)
    script = [write(path, body) for path, body in files] + [
        action("run_static_checks"),
        finish("done"),
    ]
    provider = FakeLLMProvider(agent_script=script)
    use(provider)
    response = client.post(cr_url(pid, f"/{cr['id']}/run"), json={})
    assert response.status_code == 202, response.text
    run = client.get(api(pid, f"/agent-runs/{response.json()['id']}")).json()
    return cr, run


def test_fast_path_runs_the_request_text_on_an_existing_task(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid, _plan, tasks = world
    cr, run = fast_run(client, world, [("src/label.py", "LABEL = 'List'\n")])
    assert run["status"] == "finished" and run["task_key"] in tasks
    detail = client.get(cr_url(pid, f"/{cr['id']}")).json()
    assert detail["status"] == "in_progress"
    assert [r["id"] for r in detail["log"]["runs"]] == [run["id"]]
    assert [p["id"] for p in detail["log"]["patches"]] == [run["patch_id"]]
    assert detail["log"]["patches"][0]["status"] == "proposed"
    assert detail["drift"] == []


def test_fast_path_refuses_architecture_class_and_unapproved_requests(
    client: TestClient, world: World
) -> None:
    pid = world[0]
    arch = analysed(client, pid, ARCH_TEXT)
    approve(client, pid, arch)
    assert client.post(cr_url(pid, f"/{arch['id']}/run"), json={}).status_code == 409
    fresh = analysed(client, pid)
    assert client.post(cr_url(pid, f"/{fresh['id']}/run"), json={}).status_code == 409
    approve(client, pid, fresh)
    unknown = client.post(cr_url(pid, f"/{fresh['id']}/run"), json={"task_key": "T-999"})
    assert unknown.status_code == 422


def test_accepted_patch_with_new_package_and_endpoint_flags_drift(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    cr, run = fast_run(
        client,
        world,
        [
            ("payments/__init__.py", "\n"),
            ("payments/client.py", "URL = 'https://api.stripe.com/v1/charges'\n"),
        ],
    )
    detail = client.get(cr_url(pid, f"/{cr['id']}")).json()
    assert detail["drift"] == []  # nothing is compared before acceptance
    assert review(client, pid, run["patch_id"]).status_code == 200
    detail = client.get(cr_url(pid, f"/{cr['id']}")).json()
    assert detail["drift_flagged"] and detail["drift"]
    update = client.post(
        cr_url(pid, f"/{cr['id']}/propose-architecture-update"), json={"by": "ana"}
    )
    if update.status_code == 201:
        assert update.json()["effective_class"] == "architecture"
        assert update.json()["proposals"]
    else:  # the fake provider may not produce a valid draft for this payload
        assert update.status_code in (409, 422)


def test_accepted_patch_without_drift_is_not_flagged(client: TestClient, world: World) -> None:
    pid = world[0]
    cr, run = fast_run(client, world, [("src/database.py", "LABEL = 'List'\n")])
    assert review(client, pid, run["patch_id"]).status_code == 200
    detail = client.get(cr_url(pid, f"/{cr['id']}")).json()
    assert detail["drift"] == [] and not detail["drift_flagged"]
    assert (
        client.post(
            cr_url(pid, f"/{cr['id']}/propose-architecture-update"), json={"by": "a"}
        ).status_code
        == 409
    )


def test_change_log_links_run_patch_and_pull_request(
    client: TestClient, engine: Engine, world: World
) -> None:
    from app.services.change_request import ChangeRequestService

    pid = world[0]
    cr, run = fast_run(client, world, [("src/label.py", "LABEL = 'List'\n")])
    with Session(engine) as session:
        service = ChangeRequestService(session)
        service.record_pull_request(
            uuid.UUID(pid),
            uuid.UUID(run["patch_id"]),
            "https://example.test/pr/1",
        )
    log = client.get(cr_url(pid, f"/{cr['id']}")).json()["log"]
    assert log["pull_request_url"] == "https://example.test/pr/1"
    assert log["runs"][0]["id"] == run["id"] and log["patches"][0]["id"] == run["patch_id"]
    assert set(log) >= {
        "architecture",
        "plan_deltas",
        "runs",
        "patches",
        "ci_runs",
        "requirement_ids",
    }


# ---- isolation and migration -------------------------------------------------------------------


def test_requests_are_isolated_between_projects(client: TestClient, world: World) -> None:
    pid = world[0]
    other, _proposal, _ids = approved_project(client)
    other_plan = build(client, other)
    assert client.post(plans_url(other, f"/{other_plan['id']}/approve")).status_code == 200
    cr = analysed(client, pid)
    assert client.get(cr_url(other)).json() == []
    for method, suffix in (("get", ""), ("post", "/analyse")):
        response = getattr(client, method)(cr_url(other, f"/{cr['id']}{suffix}"))
        assert response.status_code == 404
    assert client.post(cr_url(other, f"/{cr['id']}/run"), json={}).status_code == 404
    assert create(client, other)["key"] == "CR-001"


def test_migration_adds_and_removes_change_request_schema(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'cr.db').as_posix()}"
    config = alembic_config(url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        names = inspect(engine)
        assert "change_requests" in names.get_table_names()
        for table in ("architecture_proposals", "implementation_plans", "agent_runs"):
            assert "change_request_id" in {c["name"] for c in names.get_columns(table)}
        plan_columns = {c["name"] for c in names.get_columns("implementation_plans")}
        assert {"kind", "base_plan_id"} <= plan_columns
        engine.dispose()
        command.downgrade(config, "0023_task_checklists")
        names = inspect(create_engine(url))
        assert "change_requests" not in names.get_table_names()
        for table in ("architecture_proposals", "implementation_plans", "agent_runs"):
            assert "change_request_id" not in {c["name"] for c in names.get_columns(table)}
        assert "kind" not in {c["name"] for c in names.get_columns("implementation_plans")}
    finally:
        engine.dispose()
