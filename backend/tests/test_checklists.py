"""Phase 13D: task decomposition before coding (checklist of coding steps)."""

import json
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.api import deps
from app.domain.checklist import (
    GROUP_MAX_CRITERIA,
    MAX_STEPS,
    Candidates,
    ChecklistInvalidError,
    ExpectedFile,
    RelatedItem,
    Step,
    build_steps,
    order_steps,
    step_warnings,
    validate_checklist,
)
from app.llm.contracts import LLMRequest, LLMResponse
from app.llm.exceptions import LLMUnavailableError
from app.llm.fake_provider import FakeLLMProvider
from app.models.agent import AgentDraftFile, AgentRun
from app.services import agent as agent_service
from app.tools import get_registry
from tests.test_agent import ScriptedChecker, action, context_of, finish, write
from tests.test_patches import World, api, create, review, set_task, task_url
from tests.test_patches import world as _world_fixture
from tests.test_plans import approved_project, build, plans_url
from tests.test_requirements_api import use

world = _world_fixture

CRITERIA = [
    "Store each Invoice with an amount and a due date",
    "Mark an Invoice as paid",
    "List the overdue invoices",
]


@pytest.fixture(autouse=True)
def clean_agent_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("app.api.agent._spawn", lambda job: job())  # run inline
    real = deps.make_agent_service
    monkeypatch.setattr(
        "app.api.checklists.make_agent_service",
        lambda s, p: real(s, p, checker=ScriptedChecker([0])),  # type: ignore[arg-type]
    )
    yield
    agent_service._active.clear()
    if agent_service._agent_lock.locked():
        agent_service._agent_lock.release()


def key_of(world: World) -> str:
    return next(iter(world[2]))


def url(world: World, suffix: str = "", pid: str | None = None) -> str:
    _, plan, _ = world
    return task_url(pid or world[0], plan["id"], key_of(world), f"/checklist{suffix}")


def with_criteria(engine: Engine, world: World, criteria: list[str] = CRITERIA) -> None:
    task = world[2][key_of(world)]
    set_task(engine, task["id"], acceptance_criteria=criteria, depends_on=[])


def proposed(client: TestClient, world: World) -> dict[str, Any]:
    response = client.post(url(world))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def decide(client: TestClient, world: World, what: str = "approve", by: str = "ana") -> Response:
    response: Response = client.post(url(world, f"/{what}"), json={"by": by, "reason": "ok"})
    return response


def steps_in(body: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = body["checklist"]["steps"]
    return steps


EDITABLE = (
    "key",
    "title",
    "goal",
    "acceptance_criteria",
    "expected_files",
    "depends_on",
    "grouping_reason",
    "discovery_note",
    "shared_criteria",
)


def editable(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: s[k] for k in EDITABLE} for s in steps]


def split(criteria: list[str]) -> list[dict[str, Any]]:
    """One step per criterion, each depending on the one before (a person's revision)."""
    return [
        {
            "key": f"S-{n:02d}",
            "title": c,
            "goal": f"Make this hold: {c}",
            "acceptance_criteria": [c],
            "expected_files": [],
            "depends_on": [f"S-{n - 1:02d}"] if n > 1 else [],
            "discovery_note": "File discovery happens during the run.",
        }
        for n, c in enumerate(criteria, start=1)
    ]


def revise(
    client: TestClient,
    world: World,
    steps: list[dict[str, Any]],
    manual: list[dict[str, str]] | None = None,
) -> Response:
    body = {"author": "ana", "reason": "split", "steps": steps}
    if manual is not None:
        body["manual_verification"] = manual
    response: Response = client.put(url(world), json=body)
    return response


def problem_codes(response: Response) -> set[str]:
    assert response.status_code == 422, response.text
    return {p["code"] for p in response.json()["detail"]["problems"]}


def body_for(criterion: str) -> str:
    return f'"""{criterion}."""\n\n\nclass Invoice:\n    paid = False\n    overdue = False\n'


def step_reply(path: str, criterion: str) -> list[str]:
    return [write(path, body_for(criterion)), action("run_static_checks"), finish("step done")]


def approved_two_steps(client: TestClient, engine: Engine, world: World) -> None:
    with_criteria(engine, world, CRITERIA[:2])
    proposed(client, world)
    saved = revise(client, world, split(CRITERIA[:2]))
    assert saved.status_code == 200, saved.text
    assert decide(client, world).status_code == 200


def run_next(client: TestClient, world: World, **body: Any) -> Response:
    response: Response = client.post(url(world, "/run-next"), json=body)
    return response


def run_of(client: TestClient, world: World, response: Response) -> dict[str, Any]:
    assert response.status_code == 202, response.text
    found: dict[str, Any] = client.get(api(world[0], f"/agent-runs/{response.json()['id']}")).json()
    return found


def view(client: TestClient, world: World) -> dict[str, Any]:
    response = client.get(url(world))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---- domain ------------------------------------------------------------------------------------

LEDGER = "Export the Ledger to CSV"
MAPPED = Candidates(
    repository_files={
        "src/invoice.py": ["Invoice", "mark_paid", "due_date"],
        "src/ledger_export.py": ["export_csv", "Ledger"],
    },
    graph_items=[
        RelatedItem("c2", "component", "Ledger export"),
        RelatedItem("c1", "component", "Invoice service"),
        RelatedItem("r1", "requirement", "Invoices are tracked"),
    ],
    repository="mapped",
)


def covered(steps: list[Step]) -> list[str]:
    return [c for s in steps for c in s.acceptance_criteria]


def test_build_groups_criteria_into_cohesive_steps_deterministically() -> None:
    criteria = [CRITERIA[0], CRITERIA[1], LEDGER, CRITERIA[2]]
    first = build_steps("T-002", "Invoices", criteria, MAPPED)
    assert first == build_steps("T-002", "Invoices", list(criteria), MAPPED)
    steps = first.steps
    assert [s.key for s in steps] == ["S-01", "S-02"]  # not one step per criterion
    assert steps[0].acceptance_criteria == (CRITERIA[0], CRITERIA[1], CRITERIA[2])
    assert steps[1].acceptance_criteria == (LEDGER,)
    assert sorted(covered(steps)) == sorted(criteria) and first.manual == []
    assert "the same component (Invoice service)" in steps[0].grouping_reason
    assert "substantially the same files" in steps[0].grouping_reason
    assert steps[1].grouping_reason.startswith("One criterion")
    assert steps[0].expected_files == (ExpectedFile("src/invoice.py", "repository"),)
    assert steps[1].expected_files == (ExpectedFile("src/ledger_export.py", "repository"),)
    assert steps[1].depends_on == ()  # no shared file, no invented order
    assert all(step_warnings(s) == [] for s in steps)
    known = set(MAPPED.repository_files)
    assert all(f.path in known for s in steps for f in s.expected_files)  # no invented path


def test_different_prerequisites_keep_criteria_apart() -> None:
    candidates = Candidates(
        dependency_files={"src/money.py": "T-001", "src/tax.py": "T-003"}, repository="none"
    )
    steps = build_steps(
        "T-004", "Totals", ["Round the money amount", "Round the tax amount"], candidates
    ).steps
    assert [len(s.acceptance_criteria) for s in steps] == [1, 1]
    assert steps[0].expected_files == (ExpectedFile("src/money.py", "dependency"),)
    assert "Builds on the accepted files of T-001" in steps[0].grouping_reason
    assert "Builds on the accepted files of T-003" in steps[1].grouping_reason


def test_without_a_repository_files_are_discovered_during_the_run() -> None:
    result = build_steps("T-002", "Invoices", [*CRITERIA, "Log every login attempt"], Candidates())
    first, second = result.steps
    assert first.acceptance_criteria == tuple(CRITERIA)
    assert "the same subject (invoice)" in first.grouping_reason
    assert first.expected_files == () and second.expected_files == ()
    assert "No repository is connected" in first.discovery_note
    assert second.depends_on == ("S-01",)
    assert any("discovery during the run" in w for w in result.warnings)
    assert all(step_warnings(s) == [] for s in result.steps)


def test_a_step_covers_at_most_five_criteria() -> None:
    criteria = [f"Invoice rule {word}" for word in "abcdefg"]
    steps = build_steps("T-002", "Rules", criteria, Candidates()).steps
    assert [len(s.acceptance_criteria) for s in steps] == [GROUP_MAX_CRITERIA, 2]
    assert covered(steps) == criteria
    assert steps[1].grouping_reason.startswith("Grouped")
    lone = build_steps("T-002", "Rules", [*criteria[:5], "Invoice audit"], Candidates()).steps
    assert lone[1].grouping_reason.startswith("Continues S-01")


def test_manual_verification_is_only_taken_from_explicit_wording() -> None:
    criteria = [
        CRITERIA[0],
        "The invoice layout is manually verified by finance",
        "Finance gives sign-off on the invoice template",
    ]
    result = build_steps("T-002", "Invoices", criteria, Candidates())
    assert covered(result.steps) == [CRITERIA[0]]
    assert [m.criterion for m in result.manual] == criteria[1:]
    assert all(m.reason for m in result.manual)
    only_manual = build_steps("T-002", "Invoices", criteria[1:], Candidates())
    assert covered(only_manual.steps) == [] and len(only_manual.manual) == 2


def test_build_without_criteria_invents_nothing_and_merges_many_groups() -> None:
    result = build_steps("T-001", "Setup", [], Candidates())
    assert len(result.steps) == 1 and result.steps[0].acceptance_criteria == ()
    assert any("no criterion was invented" in w for w in result.warnings)
    many = [f"Zone{n}" for n in range(MAX_STEPS + 5)]  # no shared subject: one group each
    merged = build_steps("T-001", "Big", many, Candidates())
    assert len(merged.steps) <= MAX_STEPS
    assert covered(merged.steps) == many
    assert any("merged" in w for w in merged.warnings)


def test_step_quality_warnings() -> None:
    files = tuple(ExpectedFile(f"src/f{n}.py", "new") for n in range(6))
    crit = tuple(f"c{n}" for n in range(6))
    codes = step_warnings(Step("S-01", "Big", "", crit, files))
    assert len(codes) == 3 and any("6 files" in w for w in codes)
    assert any("6 criteria" in w for w in codes) and any("no goal" in w for w in codes)
    assert any("neither expected files" in w for w in step_warnings(Step("S-02", "t", "g", ())))
    noted = Step("S-03", "t", "g", (), discovery_note="Discover during the run.")
    assert step_warnings(noted) == []


def test_ordering_is_dependencies_first_then_position() -> None:
    steps = [
        Step("S-01", "a", "a", (), depends_on=("S-03",)),
        Step("S-02", "b", "b", ()),
        Step("S-03", "c", "c", ()),
    ]
    assert [s.key for s in order_steps(steps)] == ["S-02", "S-03", "S-01"]
    assert [s.key for s in order_steps(list(steps))] == ["S-02", "S-03", "S-01"]


def raw(key: str, crits: list[str], deps: Sequence[str] = (), **extra: Any) -> dict[str, Any]:
    return {
        "key": key,
        "title": key,
        "goal": "g",
        "acceptance_criteria": crits,
        "depends_on": list(deps),
        **extra,
    }


def codes_of(*args: Any, **kwargs: Any) -> set[str]:
    with pytest.raises(ChecklistInvalidError) as found:
        validate_checklist(*args, **kwargs)
    return {p.code for p in found.value.problems}


def test_validation_rejects_cycles_and_bad_dependencies() -> None:
    with pytest.raises(ChecklistInvalidError) as cycle:
        validate_checklist(
            [
                raw("S-01", ["a"], ["S-03"]),
                raw("S-02", ["b"], ["S-01"]),
                raw("S-03", ["c"], ["S-02"]),
            ],
            [],
            ["a", "b", "c"],
        )
    assert [p.code for p in cycle.value.problems] == ["dependency_cycle"]
    assert "S-01" in cycle.value.problems[0].message and "->" in cycle.value.problems[0].message
    assert codes_of(
        [raw("S-01", ["a"], ["S-01"]), raw("S-02", ["b"], ["S-09"]), raw("S-2", ["x"])],
        [],
        ["a", "b", "c"],
    ) == {
        "self_dependency",
        "unknown_dependency",
        "invalid_key",
        "invented_criterion",
        "uncovered_criterion",
    }


def test_validation_of_repeats_manual_checks_and_paths() -> None:
    criteria = ["a", "b", "c"]
    repeated = [raw("S-01", ["a", "b"]), raw("S-02", ["b", "c"])]
    assert codes_of(repeated, [], criteria) == {"repeated_criterion"}
    reasoned = [
        raw("S-01", ["a", "b"]),
        raw("S-02", ["b", "c"], shared_criteria=[{"criterion": "b", "reason": "re-check"}]),
    ]
    assert len(validate_checklist(reasoned, [], criteria).steps) == 2
    foreign = [raw("S-01", ["a", "b", "c"], shared_criteria=[{"criterion": "x", "reason": "r"}])]
    assert codes_of(foreign, [], criteria) == {"invalid_shared_criterion"}

    two = [raw("S-01", ["a", "b"])]
    result = validate_checklist(two, [{"criterion": " c ", "reason": "a person checks"}], criteria)
    assert [m.criterion for m in result.manual] == ["c"]
    assert codes_of(two, [{"criterion": "c", "reason": ""}], criteria) == {
        "manual_reason_missing",
        "uncovered_criterion",
    }
    assert codes_of([raw("S-01", criteria)], [{"criterion": "c", "reason": "r"}], criteria) == {
        "manual_and_step"
    }
    assert codes_of([raw("S-01", criteria)], [{"criterion": "z", "reason": "r"}], criteria) == {
        "invented_criterion"
    }

    known = {"src/a.py": "repository", "src/t.py": "task"}
    files = [
        {"path": "src/a.py", "source": "new"},  # exists: reclassified
        {"path": "src/n.py", "source": "new"},
        {"path": "src/legacy.py", "source": "manual"},  # old source name: a new file
    ]
    ok = validate_checklist([raw("S-01", criteria, expected_files=files)], [], criteria, (), known)
    assert ok.steps[0].expected_files == (
        ExpectedFile("src/a.py", "repository"),
        ExpectedFile("src/n.py", "new"),
        ExpectedFile("src/legacy.py", "new"),
    )
    ghost = [raw("S-01", criteria, expected_files=[{"path": "src/ghost.py", "source": "task"}])]
    assert codes_of(ghost, [], criteria, (), known) == {"unknown_path"}
    assert validate_checklist(ghost, [], criteria).steps  # approval re-check skips paths
    assert validate_checklist([raw("S-01", criteria, goal="")], [], criteria).steps


# ---- API: proposal, revision, approval ---------------------------------------------------------


def test_proposal_is_cohesive_deterministic_and_versioned(
    client: TestClient, engine: Engine, world: World
) -> None:
    with_criteria(engine, world)
    assert view(client, world)["checklist"] is None
    first = proposed(client, world)
    second = proposed(client, world)
    assert editable(steps_in(first)) == editable(steps_in(second))
    steps = steps_in(second)
    assert len(steps) < len(CRITERIA)  # grouped, not one step per criterion
    assert sorted(c for s in steps for c in s["acceptance_criteria"]) == sorted(CRITERIA)
    assert [s["criteria_count"] for s in steps] == [len(s["acceptance_criteria"]) for s in steps]
    assert all(s["grouping_reason"] for s in steps)
    assert all(s["expected_files"] == [] for s in steps)  # no repository: nothing invented
    assert all("No repository is connected" in s["discovery_note"] for s in steps)
    assert all(s["warnings"] == [] for s in steps)
    assert all(s["status"] == "todo" for s in steps)
    assert second["checklist"]["manual_verification"] == []
    assert second["checklist"]["version"] == 2 and second["checklist"]["proposed_by"] == "system"
    assert [(h["version"], h["status"]) for h in second["history"]] == [
        (2, "proposed"),
        (1, "superseded"),
    ]
    assert second["next_step"] is None  # nothing runs before approval


def test_proposal_uses_the_repository_code_map(
    client: TestClient, engine: Engine, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapped = {
        "available": True,
        "files": {"src/billing/invoice.py": ["Invoice", "mark_paid"], "src/auth.py": ["login"]},
        "other_paths": ["README.md"],
        "source_roots": ["src"],
    }
    monkeypatch.setattr(
        "app.services.agent.AgentService.repository_files", lambda self, pid: mapped
    )
    with_criteria(engine, world)
    steps = steps_in(proposed(client, world))
    paths = {f["path"] for s in steps for f in s["expected_files"]}
    assert paths == {"src/billing/invoice.py"}
    assert {f["source"] for s in steps for f in s["expected_files"]} == {"repository"}
    edited = editable(steps)
    edited[0]["expected_files"] = [{"path": "src/billing/ghost.py", "source": "repository"}]
    assert problem_codes(revise(client, world, edited)) == {"unknown_path"}
    edited[0]["expected_files"] = [
        {"path": "README.md", "source": "repository"},
        {"path": "src/billing/reminders.py", "source": "new"},
    ]
    saved = revise(client, world, edited)
    assert saved.status_code == 200, saved.text
    assert steps_in(saved.json())[0]["expected_files"][1]["source"] == "new"

    monkeypatch.setattr(
        "app.services.agent.AgentService.repository_files",
        lambda self, pid: {"available": False, "reason": "clone missing"},
    )
    unavailable = steps_in(proposed(client, world))
    assert all("unavailable" in s["discovery_note"] for s in unavailable)


def test_person_revision_is_validated(client: TestClient, engine: Engine, world: World) -> None:
    with_criteria(engine, world)
    proposed(client, world)
    steps = split(CRITERIA)
    invented = [dict(s) for s in steps]
    invented[0]["acceptance_criteria"] = [*steps[0]["acceptance_criteria"], "Send reminder emails"]
    assert problem_codes(revise(client, world, invented)) == {"invented_criterion"}
    dropped = [dict(s) for s in steps]
    dropped[2]["acceptance_criteria"] = []
    assert problem_codes(revise(client, world, dropped)) == {"uncovered_criterion"}
    unknown = [dict(s) for s in steps]
    unknown[1]["depends_on"] = ["S-07"]
    assert problem_codes(revise(client, world, unknown)) == {"unknown_dependency"}
    cyclic = [dict(s) for s in steps]
    cyclic[0]["depends_on"] = ["S-03"]
    assert problem_codes(revise(client, world, cyclic)) == {"dependency_cycle"}
    repeated = [dict(s) for s in steps]
    repeated[1]["acceptance_criteria"] = [CRITERIA[1], CRITERIA[0]]
    assert problem_codes(revise(client, world, repeated)) == {"repeated_criterion"}
    no_reason = [{"criterion": CRITERIA[2], "reason": " "}]
    assert "manual_reason_missing" in problem_codes(revise(client, world, dropped, no_reason))
    assert view(client, world)["checklist"]["version"] == 1  # nothing was saved

    reordered = [dict(s) for s in steps]
    reordered[1]["depends_on"], reordered[2]["depends_on"] = [], ["S-01", "S-02"]
    saved = revise(client, world, reordered)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert [s["key"] for s in steps_in(body)] == ["S-01", "S-02", "S-03"]
    assert body["checklist"]["proposed_by"] == "ana" and body["checklist"]["status"] == "proposed"

    manual = [{"criterion": CRITERIA[2], "reason": "Finance checks the report by hand"}]
    with_manual = revise(client, world, dropped[:2], manual)
    assert with_manual.status_code == 200, with_manual.text
    assert with_manual.json()["checklist"]["manual_verification"] == manual
    assert len(steps_in(with_manual.json())) == 2


def test_agent_proposes_but_never_approves(
    client: TestClient, engine: Engine, world: World
) -> None:
    with_criteria(engine, world)
    proposed(client, world)
    use(FakeLLMProvider())
    revised = client.post(url(world, "/agent-revision"), json={"instruction": "keep it"})
    assert revised.status_code == 200, revised.text
    checklist = revised.json()["checklist"]
    assert (checklist["proposed_by"], checklist["status"], checklist["version"]) == (
        "agent",
        "proposed",
        2,
    )
    steps = editable(steps_in(revised.json()))
    steps[0]["acceptance_criteria"] = ["Delete every invoice"]
    use(FakeLLMProvider(checklist_text=json.dumps({"steps": steps})))
    invented = client.post(url(world, "/agent-revision"), json={})
    assert "invented_criterion" in problem_codes(invented)
    for name in ("agent", "System", "  AI "):
        refused = decide(client, world, by=name)
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "invalid_decider"
    assert view(client, world)["checklist"]["status"] == "proposed"
    names = {t.name for t in get_registry().all()}
    assert not any("checklist" in n for n in names)  # no tool can approve a checklist


def legacy_patch(client: TestClient, world: World, path: str) -> Response:
    pid, plan, _ = world
    use(FakeLLMProvider(patch_text=json.dumps({"files": [create(path, body_for(CRITERIA[0]))]})))
    response: Response = client.post(task_url(pid, plan["id"], key_of(world), "/patches"), json={})
    return response


def test_legacy_actions_follow_the_checklist_status(
    client: TestClient, engine: Engine, world: World
) -> None:
    with_criteria(engine, world)
    pid, plan, _ = world
    proposed(client, world)
    allowed = legacy_patch(client, world, "src/legacy.py")  # proposed: legacy still allowed
    assert allowed.status_code == 201, allowed.text
    assert review(client, pid, allowed.json()["id"]).status_code == 200
    blocked = run_next(client, world)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "checklist_not_approved"

    assert decide(client, world, "reject").status_code == 200
    again = legacy_patch(client, world, "src/legacy_two.py")  # rejected: legacy allowed
    assert again.status_code == 201, again.text
    assert review(client, pid, again.json()["id"]).status_code == 200

    proposed(client, world)
    assert decide(client, world).status_code == 200
    body = view(client, world)
    assert body["checklist"]["read_only"] is True and body["next_step"] == "S-01"
    assert body["checklist"]["decided_by"] == "ana"
    for response in (
        revise(client, world, editable(steps_in(body))),
        client.post(url(world)),
        client.post(url(world, "/agent-revision"), json={}),
        decide(client, world),
        decide(client, world, "reject"),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "checklist_read_only"
    task_run = client.post(task_url(pid, plan["id"], key_of(world), "/agent-runs"), json={})
    assert task_run.json()["detail"]["code"] == "use_checklist"
    patch = legacy_patch(client, world, "src/legacy_three.py")
    assert patch.status_code == 409 and patch.json()["detail"]["code"] == "use_checklist"


def test_approval_waits_for_a_running_task_level_run(
    client: TestClient, engine: Engine, world: World
) -> None:
    with_criteria(engine, world)
    pid, plan, _ = world
    proposed(client, world)
    use(FakeLLMProvider(agent_script=[finish()]))
    started = client.post(task_url(pid, plan["id"], key_of(world), "/agent-runs"), json={})
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["id"])
    with Session(engine) as session:
        session.execute(update(AgentRun).where(AgentRun.id == run_id).values(status="running"))
        session.commit()
    agent_service._active.add(run_id)
    refused = decide(client, world)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "legacy_run_active"
    agent_service._active.discard(run_id)
    assert decide(client, world).status_code == 200


def test_rejected_checklist_needs_a_new_proposal(
    client: TestClient, engine: Engine, world: World
) -> None:
    with_criteria(engine, world)
    proposed(client, world)
    assert decide(client, world, "reject").status_code == 200
    assert view(client, world)["checklist"]["status"] == "rejected"
    assert decide(client, world).json()["detail"]["code"] == "checklist_not_proposed"
    assert run_next(client, world).json()["detail"]["code"] == "checklist_not_approved"
    assert proposed(client, world)["checklist"]["status"] == "proposed"


# ---- API: running steps ------------------------------------------------------------------------


def test_steps_run_one_at_a_time_on_the_accepted_files(
    client: TestClient, engine: Engine, world: World
) -> None:
    approved_two_steps(client, engine, world)
    pid = world[0]
    first = FakeLLMProvider(agent_script=step_reply("src/invoice.py", CRITERIA[0]))
    use(first)
    run1 = run_of(client, world, run_next(client, world))
    assert run1["status"] == "finished" and run1["patch_id"], run1
    assert context_of(first.requests[0].prompt)["task"]["acceptance_criteria"] == [CRITERIA[0]]
    state = view(client, world)
    assert [s["status"] for s in steps_in(state)] == ["proposed", "todo"]
    assert steps_in(state)[0]["patch"]["status"] == "proposed"
    assert steps_in(state)[0]["checks"]["finding_total"] == 0
    busy = run_next(client, world)
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "step_in_progress"

    assert review(client, pid, run1["patch_id"]).status_code == 200
    assert view(client, world)["next_step"] == "S-02"
    second = FakeLLMProvider(
        agent_script=[
            action("read_file", path="src/invoice.py"),
            *step_reply("src/paid.py", CRITERIA[1]),
        ]
    )
    use(second)
    run2 = run_of(client, world, run_next(client, world))
    assert run2["status"] == "finished", run2
    assert run2["steps"][0]["error_code"] is None  # step 1's file is in the starting workspace
    brief = context_of(second.requests[0].prompt)["task"]
    assert brief["acceptance_criteria"] == [CRITERIA[1]]
    assert brief["checklist_step"]["key"] == "S-02"
    assert brief["checklist_step"]["accepted_earlier_steps"] == [
        {"key": "S-01", "title": CRITERIA[0], "files": ["src/invoice.py"]}
    ]
    patch = client.get(api(pid, f"/patches/{run2['patch_id']}")).json()
    assert [f["path"] for f in patch["files"]] == ["src/paid.py"]
    assert any("Checklist step S-02" in n for n in patch["notes"])
    assert review(client, pid, run2["patch_id"]).status_code == 200
    done = view(client, world)
    assert [s["status"] for s in steps_in(done)] == ["accepted", "accepted"]
    assert done["next_step"] is None
    assert run_next(client, world).json()["detail"]["code"] == "checklist_complete"


def stop_after(engine: Engine, replies: list[str], calls: int) -> FakeLLMProvider:
    class StopAfter(FakeLLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            response = super().generate(request)
            if len(self.requests) == calls:
                with Session(engine) as session:
                    session.execute(
                        update(AgentRun)
                        .where(AgentRun.status == "running")
                        .values(stop_requested=True)
                    )
                    session.commit()
            return response

    provider = StopAfter(agent_script=replies)
    use(provider)
    return provider


def draft_paths(engine: Engine, run_id: str) -> dict[str, str]:
    with Session(engine) as session:
        rows = session.scalars(
            select(AgentDraftFile).where(AgentDraftFile.run_id == uuid.UUID(run_id))
        ).all()
        return {r.path: r.content for r in rows if not r.deleted}


def test_a_stopped_step_resumes_from_its_checkpoint(
    client: TestClient, engine: Engine, world: World
) -> None:
    approved_two_steps(client, engine, world)
    stop_after(engine, [write("src/invoice.py", body_for(CRITERIA[0])), finish()], calls=1)
    stopped = run_of(client, world, run_next(client, world))
    assert (stopped["status"], stopped["stop_reason"]) == ("stopped", "user_stop")
    assert "src/invoice.py" in draft_paths(engine, stopped["id"])  # the checkpoint is kept
    step = steps_in(view(client, world))[0]
    assert (step["status"], step["resumable"]) == ("stopped", True)
    assert run_next(client, world).json()["detail"]["code"] == "resume_available"
    later = client.post(url(world, "/steps/S-02/resume"), json={})
    assert later.json()["detail"]["code"] == "not_next_step"

    provider = FakeLLMProvider(agent_script=[action("run_static_checks"), finish("resumed")])
    use(provider)
    resumed = run_of(client, world, client.post(url(world, "/steps/S-01/resume"), json={}))
    assert resumed["status"] == "finished" and resumed["patch_id"], resumed
    context = context_of(provider.requests[0].prompt)["task"]
    assert context["checkpoint"]["resumed_from_run"] == stopped["id"]
    assert context["checkpoint"]["changed"] == [{"path": "src/invoice.py", "change": "create"}]
    assert draft_paths(engine, stopped["id"]) == {}  # the old checkpoint is retired
    patch = client.get(api(world[0], f"/patches/{resumed['patch_id']}")).json()
    assert [f["path"] for f in patch["files"]] == ["src/invoice.py"]
    with Session(engine) as session:
        row = session.get(AgentRun, uuid.UUID(resumed["id"]))
        assert row is not None and str(row.resumed_from_run_id) == stopped["id"]


def test_a_failed_step_resumes_and_restart_discards_the_checkpoint(
    client: TestClient, engine: Engine, world: World
) -> None:
    approved_two_steps(client, engine, world)

    class FailSecond(FakeLLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            if len(self.requests) >= 1:
                self.requests.append(request)
                raise LLMUnavailableError()
            return super().generate(request)

    use(FailSecond(agent_script=[write("src/invoice.py", body_for(CRITERIA[0]))]))
    failed = run_of(client, world, run_next(client, world))
    assert (failed["status"], failed["stop_reason"]) == ("failed", "provider_error")
    assert steps_in(view(client, world))[0]["resumable"] is True

    use(FakeLLMProvider(agent_script=step_reply("src/other.py", CRITERIA[0])))
    restarted = run_of(client, world, run_next(client, world, restart=True))
    assert restarted["status"] == "finished"
    assert draft_paths(engine, failed["id"]) == {}
    patch = client.get(api(world[0], f"/patches/{restarted['patch_id']}")).json()
    assert [f["path"] for f in patch["files"]] == ["src/other.py"]  # nothing from the old draft


def test_a_stale_checkpoint_cannot_resume(client: TestClient, engine: Engine, world: World) -> None:
    approved_two_steps(client, engine, world)
    stop_after(engine, [write("src/invoice.py", "x = 1\n"), finish()], calls=1)
    stopped = run_of(client, world, run_next(client, world))
    assert stopped["status"] == "stopped"
    # Meanwhile a legacy patch of another task (no checklist) adds the same path.
    pid, plan, tasks = world
    other = [k for k in tasks if k != key_of(world)][0]
    set_task(engine, tasks[other]["id"], depends_on=[])
    use(FakeLLMProvider(patch_text=json.dumps({"files": [create("src/invoice.py", "y = 2\n")]})))
    legacy = client.post(task_url(pid, plan["id"], other, "/patches"), json={})
    assert legacy.status_code == 201, legacy.text
    assert review(client, pid, legacy.json()["id"]).status_code == 200
    stale = client.post(url(world, "/steps/S-01/resume"), json={})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "checkpoint_stale"
    assert stale.json()["detail"]["paths"] == ["src/invoice.py"]
    assert "src/invoice.py" in draft_paths(engine, stopped["id"])  # still there to inspect


def test_checklists_are_project_scoped(client: TestClient, engine: Engine, world: World) -> None:
    with_criteria(engine, world)
    proposed(client, world)
    other, _, _ = approved_project(client)
    other_plan = build(client, other)
    assert client.post(plans_url(other, f"/{other_plan['id']}/approve")).status_code == 200
    for response in (
        client.get(url(world, pid=other)),
        client.post(url(world, pid=other)),
        client.post(url(world, "/approve", pid=other), json={"by": "ana", "reason": "ok"}),
        client.post(url(world, "/run-next", pid=other), json={}),
    ):
        assert response.status_code == 404, response.text
    mine = task_url(other, other_plan["id"], key_of(world), "/checklist")
    assert client.get(mine).json()["checklist"] is None  # the same key in the other project
    assert view(client, world)["checklist"]["status"] == "proposed"
