import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models.agent import AgentStep
from app.services.agent_hooks import HookInput, get_hooks
from app.services.hooks import ALLOW, Hook, HookRegistry, HookResult, annotate, block
from app.services.static_checks import CheckReport
from tests.test_agent import (
    ScriptedChecker,
    action,
    clean_agent_state,  # noqa: F401  (autouse fixture of the agent tests)
    configure,
    finish,
    play,
    write,
)
from tests.test_agent import world as world  # noqa: F401
from tests.test_patches import World, api

SECRET = "sk-live-ABCDEF1234567890abcdXYZ"


def hook(name: str, result: HookResult, point: Any = "before_action", **kw: Any) -> Hook:
    return Hook(name, point, lambda _ctx: result, **kw)


def stored_steps(engine: Engine) -> list[AgentStep]:
    with Session(engine) as session:
        return list(session.scalars(select(AgentStep).order_by(AgentStep.step_no)).all())


def decisions(step: dict[str, Any]) -> dict[str, str]:
    return {h["hook"]: h["result"] for h in step["hooks"]}


# ---- the registry ------------------------------------------------------------------------------


def test_hooks_run_in_order_and_filter_by_tool() -> None:
    seen: list[str] = []

    def make(name: str, order: int, tools: frozenset[str] | None = None) -> Hook:
        def run(_ctx: Any) -> HookResult:
            seen.append(name)
            return ALLOW

        return Hook(name, "before_action", run, order=order, tools=tools)

    registry = HookRegistry(
        [
            make("c", 30),
            make("a", 10),
            make("b", 10),
            make("only_write", 5, frozenset({"write_file"})),
        ]
    )
    registry.run("before_action", "read_file", None)
    assert seen == ["a", "b", "c"]
    seen.clear()
    registry.run("before_action", "write_file", None)
    assert seen == ["only_write", "a", "b", "c"]
    seen.clear()
    registry.run("after_action", "write_file", None)
    assert seen == []


def test_allow_block_and_annotate() -> None:
    registry = HookRegistry(
        [
            hook("quiet", ALLOW, order=1),
            hook("note", annotate("fyi", extra=1), order=2),
            hook("stop", block("nope", "Refused: nope", why="x"), order=3),
            hook("never", ALLOW, order=4),
        ]
    )
    outcome = registry.run("before_action", "write_file", None)
    assert [(e["hook"], e["result"]) for e in outcome.log] == [
        ("quiet", "allow"),
        ("note", "annotate"),
        ("stop", "block"),
    ]  # the block ends the chain
    assert outcome.blocked is not None and outcome.blocked.code == "nope"
    assert outcome.blocked.data == {"why": "x"}
    assert outcome.attach == {"extra": 1}


def test_required_hooks_are_active_whatever_is_disabled() -> None:
    registry = HookRegistry([hook("must", ALLOW, required=True), hook("may", ALLOW)])
    disabled = frozenset({"must", "may"})
    assert [h.name for h in registry.active("before_action", "x", disabled)] == ["must"]


def test_builtin_hooks_never_get_a_provider() -> None:
    hooks = get_hooks().all()
    assert {h.name for h in hooks if h.required} == {
        "path_rules",
        "secret_scan",
        "env_example_rule",
    }
    assert {h.point for h in hooks} == {"before_action", "after_action", "before_finish"}
    assert not any(f in HookInput.__dataclass_fields__ for f in ("provider", "llm", "model"))


# ---- in the loop -------------------------------------------------------------------------------


def test_secret_is_blocked_at_write_time_without_naming_it(
    client: TestClient, engine: Engine, world: World
) -> None:
    run, provider = play(
        client, world, [write("src/c.py", f'api_key = "{SECRET}"\n'), write("src/ok.py"), finish()]
    )
    step = run["steps"][0]
    assert (step["status"], step["error_code"]) == ("error", "secret_detected")
    assert decisions(step) == {"path_rules": "allow", "secret_scan": "block"}
    assert "src/c.py" in step["result_summary"] and "secret" in step["result_summary"]
    assert "no secrets" in step["result_summary"]
    assert "static_checks" not in decisions(step)  # nothing was written, nothing is checked
    for row in stored_steps(engine):
        assert SECRET not in json.dumps([row.args, row.result, row.result_summary, row.path])
    assert len(provider.requests) == run["steps_taken"]  # hooks never call the model


def test_automatic_checks_after_write_edit_and_delete(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    checker = ScriptedChecker([2, 1, 0])
    configure(monkeypatch, checker=checker)
    run, provider = play(
        client,
        world,
        [
            write("src/m.py", "a = 1\n"),
            action("edit_file", path="src/m.py", search="a = 1", replace="a = 2"),
            write("src/n.py", "b = 1\n"),
            action("delete_file", path="src/n.py"),
            finish(),
        ],
    )
    assert checker.calls == 4  # one per change, none for finish
    assert [decisions(s).get("static_checks") for s in run["steps"]] == [
        "annotate",
        "annotate",
        "annotate",
        "annotate",
        None,
    ]
    first = json.loads(stored_steps(engine)[0].result)
    assert first["static_checks"]["total"] == 2
    assert first["static_checks"]["findings"][0]["code"] == "F401"
    assert run["status"] == "finished"
    assert len(provider.requests) == run["steps_taken"]


def test_a_blocked_write_runs_no_checks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    checker = ScriptedChecker([0])
    configure(monkeypatch, checker=checker)
    play(client, world, [write("../evil.py"), write("src/c.py", f'token = "{SECRET}"\n'), finish()])
    assert checker.calls == 0


def test_unavailable_checker_is_reported_and_the_run_goes_on(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    class Down:
        calls = 0

        def run(self, files: Any) -> CheckReport:
            self.calls += 1
            return CheckReport([], 0, {"ruff": "unavailable", "mypy": "ok"})

    checker = Down()
    configure(monkeypatch, checker=checker)
    run, _ = play(client, world, [write("src/m.py"), write("src/k.py"), finish()])
    assert run["status"] == "finished" and checker.calls == 2
    assert run["steps"][0]["status"] == "ok"
    stored = json.loads(stored_steps(engine)[0].result)
    assert stored["static_checks"]["status"] == "unavailable"
    assert "total" not in stored["static_checks"]  # never "0 findings"


def test_explicit_check_after_an_edit_is_served_from_the_automatic_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    checker = ScriptedChecker([3, 2, 1, 0])
    configure(monkeypatch, checker=checker)
    run, _ = play(
        client,
        world,
        [
            write("src/m.py", "a = 1\n"),  # automatic check: call 1 (3 findings)
            action("run_static_checks"),  # same draft: cached, no call
            action("run_static_checks"),  # the cache is used once: call 2 (2 findings)
            action("edit_file", path="src/m.py", search="a = 1", replace="a = 2"),  # call 3
            action("run_static_checks"),  # cached again
            finish(),
        ],
    )
    assert checker.calls == 3
    totals = [s["finding_total"] for s in run["steps"]]
    assert totals == [None, 3, 2, None, 1, None]
    assert run["steps"][1]["findings"][0]["scope"] == "in changed files"


def test_automatic_checks_alone_trigger_not_converging(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    checker = ScriptedChecker([5, 5, 5, 5, 5])
    configure(monkeypatch, checker=checker)
    edits = [
        action("edit_file", path="src/m.py", search=f"a = {n}", replace=f"a = {n + 1}")
        for n in range(6)
    ]
    run, _ = play(client, world, [write("src/m.py", "a = 0\n"), *edits, finish()])
    assert (run["status"], run["stop_reason"]) == ("stopped", "not_converging")
    # 1 write (best) + 3 non-improving edits; no explicit run_static_checks was ever called.
    assert checker.calls == 4 and run["steps_taken"] == 4
    assert all(s["action"] != "run_static_checks" for s in run["steps"])


def test_a_cached_check_does_not_count_twice_for_convergence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    # Each cached explicit call repeats its automatic check; if it counted, the run would stall.
    checker = ScriptedChecker([5, 4, 3, 2])
    configure(monkeypatch, checker=checker)
    run, _ = play(
        client,
        world,
        [
            write("src/m.py", "a = 0\n"),
            action("run_static_checks"),
            action("edit_file", path="src/m.py", search="a = 0", replace="a = 1"),
            action("run_static_checks"),
            action("edit_file", path="src/m.py", search="a = 1", replace="a = 2"),
            action("run_static_checks"),
            action("edit_file", path="src/m.py", search="a = 2", replace="a = 3"),
            action("run_static_checks"),
            finish(),
        ],
    )
    assert (run["status"], run["stop_reason"]) == ("finished", "finished")
    assert checker.calls == 4


def test_the_run_keeps_the_hooks_it_started_with(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    url = api(world[0], "/agent-hooks")
    checker = ScriptedChecker([0])
    configure(monkeypatch, checker=checker)
    run, _ = play(client, world, [write("src/m.py"), finish()])
    assert {h["name"]: h["enabled"] for h in run["hooks"]}["static_checks"] is True
    client.put(f"{url}/static_checks", json={"enabled": False})
    shown = client.get(api(world[0], f"/agent-runs/{run['id']}")).json()
    assert {h["name"]: h["enabled"] for h in shown["hooks"]}["static_checks"] is True


def test_hook_decisions_are_logged_on_every_step(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    configure(monkeypatch, checker=ScriptedChecker([0]))
    run, _ = play(client, world, [action("list_files"), write("src/m.py"), finish()])
    assert run["steps"][0]["hooks"] == []  # no hook applies to a read
    assert run["steps"][1]["hooks"] == [
        {"hook": "path_rules", "point": "before_action", "result": "allow", "reason": ""},
        {"hook": "secret_scan", "point": "before_action", "result": "allow", "reason": ""},
        {"hook": "env_example_rule", "point": "before_action", "result": "allow", "reason": ""},
        {
            "hook": "static_checks",
            "point": "after_action",
            "result": "annotate",
            "reason": "0 finding(s)",
        },
    ]
    assert [h["hook"] for h in run["steps"][2]["hooks"]] == [
        "patch_validation",
        "removal_check",
        "coverage_gate",
    ]
    assert stored_steps(engine)[1].args["hooks"][0]["hook"] == "path_rules"
    assert all(h["point"] == "before_finish" for h in run["steps"][2]["hooks"])


def test_finish_blocked_by_a_hook_is_an_error_step(client: TestClient, world: World) -> None:
    run, _ = play(client, world, [finish("nothing yet"), write("src/m.py"), finish()])
    first = run["steps"][0]
    assert first["error_code"] == "patch_invalid"
    assert decisions(first) == {"patch_validation": "block"}
    assert run["status"] == "finished"


# ---- configuration -----------------------------------------------------------------------------


def test_required_hooks_cannot_be_disabled_and_optional_ones_can(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    url = api(world[0], "/agent-hooks")
    listed = {h["name"]: h for h in client.get(url).json()}
    assert listed["secret_scan"]["required"] and listed["secret_scan"]["enabled"]
    assert not listed["static_checks"]["required"]

    for name in ("path_rules", "secret_scan"):
        refused = client.put(f"{url}/{name}", json={"enabled": False})
        assert refused.status_code == 409
    assert client.put(f"{url}/nope", json={"enabled": False}).status_code == 404
    assert client.get(url).json() == list(listed.values())  # nothing changed

    off = client.put(f"{url}/static_checks", json={"enabled": False})
    assert off.status_code == 200
    assert {h["name"]: h["enabled"] for h in off.json()}["static_checks"] is False

    checker = ScriptedChecker([0])
    configure(monkeypatch, checker=checker)
    run, _ = play(client, world, [write("src/m.py"), finish()])
    assert checker.calls == 0 and "static_checks" not in decisions(run["steps"][0])
    shown = {h["name"]: h["enabled"] for h in run["hooks"]}
    assert shown["static_checks"] is False and shown["secret_scan"] is True

    again = client.put(f"{url}/static_checks", json={"enabled": True})
    assert {h["name"]: h["enabled"] for h in again.json()}["static_checks"] is True


def test_hooks_of_an_unknown_project_are_not_found(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000001"
    assert client.get(f"/api/projects/{missing}/agent-hooks").status_code == 404
    response = client.put(f"/api/projects/{missing}/agent-hooks/static_checks", json={"enabled": 0})
    assert response.status_code == 404
