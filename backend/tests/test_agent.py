import importlib.util
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.domain.agent import ActionError, Draft, DraftFile, parse_action
from app.domain.patch import content_hash
from app.domain.plan import AI_RECOMMENDATION_FLAG
from app.llm.contracts import LLMRequest, LLMResponse
from app.llm.exceptions import LLMUnavailableError
from app.llm.fake_provider import FakeLLMProvider
from app.models.agent import AgentDraftFile, AgentRun, AgentStep
from app.models.patch import CodePatch
from app.services import agent as agent_service
from app.services.static_checks import (
    MYPY_CONFIG,
    SYNTAX_ONLY_NOTE,
    CheckReport,
    CommandResult,
    Finding,
    StaticChecker,
    run_command,
)
from tests.test_patches import (
    World,
    accepted,
    api,
    create,
    review,
    set_task,
    task_url,
)
from tests.test_patches import (
    world as _world_fixture,
)
from tests.test_requirements_api import use

world = _world_fixture  # re-exported so pytest finds the fixture here


def action(name: str, **args: Any) -> str:
    return json.dumps({"action": name, "args": args})


def finish(summary: str = "done", **extra: Any) -> str:
    return action("finish", summary=summary, **extra)


def write(path: str, content: str = "x = 1\n") -> str:
    return action("write_file", path=path, content=content)


@pytest.fixture(autouse=True)
def clean_agent_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("app.api.agent._spawn", lambda job: job())  # run inline
    yield
    agent_service._active.clear()
    if agent_service._agent_lock.locked():
        agent_service._agent_lock.release()


def configure(monkeypatch: pytest.MonkeyPatch, **settings: Any) -> None:
    """Agent settings and test hooks (clock, checker) for the services the API builds."""
    hooks = {k: settings.pop(k) for k in ("clock", "checker") if k in settings}
    patched = get_settings().model_copy(update={f"agent_{k}": v for k, v in settings.items()})
    monkeypatch.setattr("app.api.deps.get_settings", lambda: patched)
    real = deps.make_agent_service
    monkeypatch.setattr("app.api.agent.make_agent_service", lambda s, p: real(s, p, **hooks))


def scripted(replies: list[str]) -> FakeLLMProvider:
    provider = FakeLLMProvider(agent_script=list(replies))
    use(provider)
    return provider


def start(client: TestClient, world: World, key: str | None = None, **body: Any) -> Response:
    pid, plan, tasks = world
    key = key or next(iter(tasks))
    response: Response = client.post(task_url(pid, plan["id"], key, "/agent-runs"), json=body)
    return response


def run_of(client: TestClient, world: World, response: Response) -> dict[str, Any]:
    assert response.status_code == 202, response.text
    found = client.get(api(world[0], f"/agent-runs/{response.json()['id']}"))
    assert found.status_code == 200, found.text
    body: dict[str, Any] = found.json()
    return body


def play(
    client: TestClient, world: World, replies: list[str], **body: Any
) -> tuple[dict[str, Any], FakeLLMProvider]:
    provider = scripted(replies)
    return run_of(client, world, start(client, world, **body)), provider


def codes(run: dict[str, Any]) -> list[str | None]:
    return [s["error_code"] for s in run["steps"]]


def context_of(prompt: str) -> dict[str, Any]:
    match = re.search(r"BEGIN_CONTEXT_(\w+)\n(.*)\nEND_CONTEXT_\1\n", prompt, re.DOTALL)
    assert match
    found: dict[str, Any] = json.loads(match.group(2))
    return found


# ---- the loop ----------------------------------------------------------------------------------


def test_successful_run_becomes_a_proposed_patch_that_a_person_accepts(
    client: TestClient, engine: Engine, world: World
) -> None:
    run, provider = play(
        client,
        world,
        [
            action("list_files"),
            write("src/hello.py", "def hello() -> str:\n    return 'hi'\n"),
            action("run_static_checks"),
            finish("Added hello", assumptions=["none"], test_suggestions=["test hello"]),
        ],
    )
    assert (run["status"], run["stop_reason"], run["steps_taken"]) == ("finished", "finished", 4)
    assert [s["action"] for s in run["steps"]] == [
        "list_files",
        "write_file",
        "run_static_checks",
        "finish",
    ]
    assert run["prompt_version"] == "agent-v1" and run["patch_id"]
    assert run["input_tokens"] > 0 and run["duration_ms"] >= 0
    check = next(s for s in run["steps"] if s["action"] == "run_static_checks")
    assert check["result_summary"] == "0 finding(s)"
    pid = world[0]
    patch = client.get(api(pid, f"/patches/{run['patch_id']}")).json()
    assert (patch["status"], patch["prompt_version"], patch["version"]) == (
        "proposed",
        "agent-v1",
        1,
    )
    assert patch["notes"] == ["Added hello"] and patch["test_suggestions"] == ["test hello"]
    assert [f["path"] for f in patch["files"]] == ["src/hello.py"]
    assert client.get(api(pid, "/workspace")).json() == {"files": []}  # nothing accepted yet
    assert review(client, pid, run["patch_id"]).status_code == 200
    assert [f["path"] for f in client.get(api(pid, "/workspace")).json()["files"]] == [
        "src/hello.py"
    ]
    assert len(provider.requests) == 4
    with Session(engine) as session:  # the draft is dropped once the run ends
        assert session.scalars(select(AgentDraftFile)).all() == []


def test_default_fake_run_finishes(client: TestClient, world: World) -> None:
    use(FakeLLMProvider())
    run = run_of(client, world, start(client, world))
    assert run["status"] == "finished" and run["steps_taken"] == 2 and run["patch_id"]


def test_invalid_actions_come_back_as_errors_not_crashes(client: TestClient, world: World) -> None:
    run, _ = play(
        client,
        world,
        [
            "this is not json",
            action("fly_to_the_moon"),
            json.dumps({"action": "read_file", "args": {"path": 5}}),
            json.dumps(["a", "list"]),
            json.dumps({"action": "read_file", "args": {"path": "a.py", "extra": 1}}),
            write("src/ok.py"),
            finish(),
        ],
    )
    assert codes(run) == [
        "invalid_json",
        "unknown_action",
        "invalid_args",
        "invalid_shape",
        "invalid_args",
        None,
        None,
    ]
    assert run["status"] == "finished"
    assert run["steps"][0]["action"] == "invalid"  # the raw action name is never stored


def test_edit_needs_exactly_one_match(client: TestClient, world: World) -> None:
    accepted(client, world, [create("src/a.py", "x = 1\nx = 1\ny = 2\n")])
    run, provider = play(
        client,
        world,
        [
            action("edit_file", path="src/a.py", search="x = 1", replace="x = 9"),
            action("edit_file", path="src/a.py", search="nothing like this", replace="z"),
            action("edit_file", path="src/missing.py", search="a", replace="b"),
            action("edit_file", path="src/a.py", search="y = 2", replace="y = 3"),
            action("read_file", path="src/a.py"),
            finish(),
        ],
    )
    assert codes(run) == ["multiple_matches", "no_match", "file_not_found", None, None, None]
    read_result = next(
        s for s in context_of(provider.requests[-1].prompt)["last_results"] if s["step"] == 5
    )
    assert "y = 3" in read_result["result"]
    patch = client.get(api(world[0], f"/patches/{run['patch_id']}")).json()
    assert [(f["path"], f["change"]) for f in patch["files"]] == [("src/a.py", "modify")]
    assert patch["files"][0]["content"] == "x = 1\nx = 1\ny = 3\n"


def test_write_and_delete_rules(client: TestClient, world: World) -> None:
    accepted(client, world, [create("src/a.py"), create("src/gone.py")])
    run, _ = play(
        client,
        world,
        [
            write("src/a.py", "x = 2\n"),  # exists
            write("src/A.PY", "x = 2\n"),  # case clash
            write("src/../up.py"),
            write("/etc/passwd.py"),
            write("src/blob.exe"),
            action("delete_file", path="src/nope.py"),
            action("delete_file", path="src/gone.py"),
            write("src/new.py"),
            finish(),
        ],
    )
    assert codes(run)[:7] == [
        "file_exists",
        "file_exists",
        "path_traversal",
        "path_absolute",
        "extension_not_allowed",
        "file_not_found",
        None,
    ]
    patch = client.get(api(world[0], f"/patches/{run['patch_id']}")).json()
    assert {(f["path"], f["change"]) for f in patch["files"]} == {
        ("src/gone.py", "delete"),
        ("src/new.py", "create"),
    }


def test_secrets_and_bad_content_are_refused_and_never_stored(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    secret = "sk-live-ABCDEF1234567890abcdXYZ"
    small = get_settings().model_copy(update={"patch_max_file_bytes": 1_000})
    monkeypatch.setattr("app.api.deps.get_settings", lambda: small)
    run, _ = play(
        client,
        world,
        [
            write("src/c.py", f'api_key = "{secret}"\n'),
            write(".env.example", "TOKEN=abc\n"),
            write("src/big.py", "x" * 1_500),
            write("src/ok.py"),
            finish(),
        ],
    )
    assert codes(run)[:3] == ["secret_detected", "env_example_value", "file_too_large"]
    with Session(engine) as session:
        for row in session.scalars(select(AgentStep)).all():
            assert secret not in json.dumps([row.args, row.result, row.result_summary, row.path])


def test_finish_validates_the_draft_and_keeps_going(client: TestClient, world: World) -> None:
    run, _ = play(
        client,
        world,
        [
            finish("nothing yet"),
            write("src/bad.py", "def broken(:\n"),
            finish("syntax"),
            action(
                "edit_file", path="src/bad.py", search="def broken(:", replace="def ok():\n    pass"
            ),
            finish("fixed"),
        ],
    )
    assert codes(run) == ["patch_invalid", None, "patch_invalid", None, None]
    assert run["status"] == "finished"
    assert client.get(api(world[0], f"/patches/{run['patch_id']}")).json()["notes"] == ["fixed"]


def test_actions_reuse_the_mcp_services(client: TestClient, world: World) -> None:
    key = next(iter(world[2]))
    run, provider = play(
        client,
        world,
        [
            action("get_requirements"),
            action("get_task"),
            action("get_task", key="T-999"),
            action("get_impact", node_id="not-a-uuid"),
            action("search_code", query="x"),
            write("src/s.py", "needle = 1\n"),
            action("search_code", query="NEEDLE"),
            finish(),
        ],
    )
    assert codes(run) == [None, None, "task_not_found", "tool_input", None, None, None, None]
    first = context_of(provider.requests[0].prompt)
    assert first["task"]["key"] == key and first["task"]["requirements"]
    seen = context_of(provider.requests[2].prompt)["last_results"]
    assert key in seen[-1]["result"]
    assert "needle" in context_of(provider.requests[-1].prompt)["last_results"][-1]["result"]


# ---- limits ------------------------------------------------------------------------------------


def test_step_limit(client: TestClient, world: World) -> None:
    run, provider = play(client, world, [action("list_files")] * 10, max_steps=3)
    assert (run["status"], run["stop_reason"], run["steps_taken"]) == ("stopped", "step_limit", 3)
    assert run["patch_id"] is None and len(provider.requests) == 3


def test_token_budget(client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    configure(monkeypatch, token_budget=1)
    run, _ = play(client, world, [action("list_files")] * 5)
    assert (run["stop_reason"], run["steps_taken"]) == ("token_budget", 1)
    assert run["patch_id"] is None


def test_wall_clock_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    ticks = iter(range(0, 10_000, 100))
    configure(monkeypatch, clock=lambda: float(next(ticks)))
    run, _ = play(client, world, [action("list_files")] * 10)
    assert (run["status"], run["stop_reason"], run["steps_taken"]) == ("stopped", "time_limit", 2)


def test_same_error_three_times_stops_the_run(client: TestClient, world: World) -> None:
    run, provider = play(
        client, world, [action("read_file", path="src/none.py")] * 3 + [finish()] * 3
    )
    assert (run["stop_reason"], run["steps_taken"]) == ("repeated_error", 3)
    assert len(provider.requests) == 3


def test_different_errors_or_a_success_reset_the_repeat_count(
    client: TestClient, world: World
) -> None:
    run, _ = play(
        client,
        world,
        [
            action("read_file", path="a.py"),
            action("read_file", path="a.py"),
            action("list_files"),
            action("read_file", path="a.py"),
            action("read_file", path="a.py"),
            write("src/k.py"),
            finish(),
        ],
    )
    assert run["status"] == "finished"


def test_user_stop_between_steps(client: TestClient, engine: Engine, world: World) -> None:
    class StopAfterFirst(FakeLLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            response = super().generate(request)
            if len(self.requests) == 1:
                with Session(engine) as session:
                    session.execute(update(AgentRun).values(stop_requested=True))
                    session.commit()
            return response

    use(StopAfterFirst(agent_script=[action("list_files")] * 5))
    run = run_of(client, world, start(client, world))
    assert (run["status"], run["stop_reason"], run["steps_taken"]) == ("stopped", "user_stop", 1)


def test_stop_endpoint_and_busy_lock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    monkeypatch.setattr("app.api.agent._spawn", lambda job: None)  # the worker never starts
    scripted([finish()])
    started = start(client, world)
    assert started.status_code == 202 and started.json()["status"] == "running"
    run_id = started.json()["id"]
    assert start(client, world).status_code == 429
    stopped = client.post(api(world[0], f"/agent-runs/{run_id}/stop"))
    assert stopped.status_code == 200 and stopped.json()["stop_requested"] is True
    agent_service._active.clear()  # the worker is gone: the run is reported as interrupted
    final = client.get(api(world[0], f"/agent-runs/{run_id}")).json()
    assert (final["status"], final["stop_reason"]) == ("failed", "interrupted")
    assert client.post(api(world[0], f"/agent-runs/{run_id}/stop")).status_code == 409


def test_provider_failure_ends_the_run_cleanly(client: TestClient, world: World) -> None:
    use(FakeLLMProvider(error=LLMUnavailableError()))
    run = run_of(client, world, start(client, world))
    assert (run["status"], run["stop_reason"]) == ("failed", "provider_error")
    assert not agent_service._agent_lock.locked()


def test_context_keeps_the_last_results_and_summarises_the_rest(
    client: TestClient, world: World
) -> None:
    _, provider = play(
        client,
        world,
        [write("src/w.py")] + [action("list_files", limit=n) for n in range(1, 6)] + [finish()],
    )
    last = context_of(provider.requests[-1].prompt)
    assert [r["step"] for r in last["last_results"]] == [4, 5, 6]
    assert len(last["earlier_steps"]) == 3 and last["earlier_steps"][0].startswith("1. write_file")
    assert last["progress"]["step"] == 7
    limit = get_settings().agent_context_chars
    assert len(provider.requests[-1].prompt) < limit + 8_000


# ---- gates -------------------------------------------------------------------------------------


def test_gates(client: TestClient, engine: Engine, world: World) -> None:
    pid, plan, tasks = world
    key, task = next(iter(tasks.items()))
    scripted([write("src/g.py"), finish()])
    set_task(engine, task["id"], flags=[AI_RECOMMENDATION_FLAG])
    blocked = start(client, world)
    assert (
        blocked.status_code == 409 and blocked.json()["detail"]["code"] == "technology_unconfirmed"
    )
    url = task_url(pid, plan["id"], key, "/technology-confirmation")
    assert client.post(url, json={"name": "Ana", "reason": "mandated"}).status_code == 201
    set_task(engine, task["id"], blocking_questions=["Which region?"])
    asked = start(client, world)
    assert asked.status_code == 409 and asked.json()["detail"]["code"] == "blocking_questions"
    with Session(engine) as session:
        assert session.scalars(select(AgentRun)).all() == []
    ok = start(client, world, acknowledge_blocking_questions=True)
    assert ok.status_code == 202
    run = client.get(api(pid, f"/agent-runs/{ok.json()['id']}")).json()
    assert run["status"] == "finished"
    assert not agent_service._agent_lock.locked()


def test_unapproved_plan_is_refused(client: TestClient, world: World) -> None:
    from tests.test_plans import build

    pid, _plan, _tasks = world
    draft = build(client, pid)
    scripted([finish()])
    response = client.post(
        task_url(pid, draft["id"], draft["tasks"][0]["key"], "/agent-runs"), json={}
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "plan_not_approved"


def test_unknown_run_and_project_isolation(client: TestClient, world: World) -> None:
    run, _ = play(client, world, [write("src/p.py"), finish()])
    other = "00000000-0000-4000-8000-000000000000"
    assert client.get(api(other, f"/agent-runs/{run['id']}")).status_code == 404
    assert client.get(api(world[0], f"/agent-runs/{uuid.uuid4()}")).status_code == 404
    assert client.post(api(other, f"/agent-runs/{run['id']}/stop")).status_code == 404
    listing = client.get(task_url(world[0], world[1]["id"], next(iter(world[2])), "/agent-runs"))
    assert [r["id"] for r in listing.json()] == [run["id"]]


# ---- static checks, disk and execution safety ---------------------------------------------------


def test_static_findings_are_fed_back_to_the_model(client: TestClient, world: World) -> None:
    run, provider = play(
        client,
        world,
        [
            write("src/m.py", "import os\n\n\ndef f() -> int:\n    return 'a'\n"),
            write("src/bad.json", "{oops"),
            action("run_static_checks"),
            finish(),
        ],
    )
    assert run["steps"][2]["result_summary"].endswith("finding(s)")
    result = context_of(provider.requests[3].prompt)["last_results"][-1]["result"]
    found = json.loads(result)
    by_code = {f["code"]: f for f in found["findings"]}
    assert by_code["F401"]["file"] == "src/m.py" and by_code["F401"]["line"] == 1
    assert by_code["F401"]["tool"] == "ruff"
    assert "return-value" in by_code and by_code["return-value"]["tool"] == "mypy"
    assert by_code["json_invalid"]["file"] == "src/bad.json"
    assert found["tools"]["ruff"] == "ok" and found["tools"]["tsc"] == "skipped_no_files"


def test_generated_config_files_are_ignored(tmp_path: Path) -> None:
    files = {
        "src/a.py": "import os\n",
        "ruff.toml": 'select = ["ALL"]\nignore = ["F401"]\n',
        "pyproject.toml": '[tool.ruff]\nignore = ["F401"]\n',
        "mypy.ini": "[mypy]\nignore_errors = True\n",
    }
    report = StaticChecker(timeout=60).run(files)
    assert {f.code for f in report.findings} >= {"F401"}


def test_no_disk_writes_outside_the_temp_dir_and_cleanup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, world: World
) -> None:
    temp = tmp_path / "tmp"
    temp.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.chdir(cwd)
    seen: list[str] = []

    def spy(argv: list[str], run_cwd: Path, env: Any, timeout: float) -> CommandResult:
        seen.append(str(run_cwd))
        assert Path(run_cwd).parent.parent == temp  # always inside the temp dir
        return CommandResult(0, "[]" if "ruff" in argv else "")

    configure(monkeypatch, checker=StaticChecker(runner=spy))
    before = {p for p in tmp_path.rglob("*")}
    run, _ = play(
        client,
        world,
        [
            write("src/d.py"),
            write("web/a.ts", "export const a = 1\n"),
            action("run_static_checks"),
            finish(),
        ],
    )
    assert run["status"] == "finished" and seen
    assert list(temp.iterdir()) == []  # the check directory is gone
    assert list(cwd.iterdir()) == []
    assert {p for p in tmp_path.rglob("*")} == before


def test_temp_dir_is_removed_after_timeout_and_after_errors(tmp_path: Path) -> None:
    temp = tmp_path / "t"
    temp.mkdir()
    roots: list[Path] = []

    def slow(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        roots.append(Path(cwd).parent)
        assert Path(cwd).parent.exists()
        return CommandResult(None, "", timed_out=True)

    report = StaticChecker(temp_parent=str(temp), runner=slow).run({"a.py": "x = 1\n"})
    assert report.tools["ruff"] == "timeout" and report.tools["mypy"] == "timeout"
    assert roots and not roots[0].exists() and list(temp.iterdir()) == []

    def boom(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        raise RuntimeError("tool crashed")

    with pytest.raises(RuntimeError):
        StaticChecker(temp_parent=str(temp), runner=boom).run({"a.py": "x = 1\n"})
    assert list(temp.iterdir()) == []


def test_subprocess_environment_has_no_secrets_and_a_dead_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "super-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    captured: list[dict[str, str]] = []

    def spy(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        captured.append(dict(env))
        return CommandResult(0, "[]" if "ruff" in argv else "")

    StaticChecker(temp_parent=str(tmp_path), runner=spy).run({"a.py": "x = 1\n"})
    assert captured
    for env in captured:
        assert "AZURE_OPENAI_API_KEY" not in env and "DATABASE_URL" not in env
        assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:")


def test_tsc_output_is_parsed_and_missing_modules_are_noise(tmp_path: Path) -> None:
    def fake_tsc(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        assert argv[0] == "tsc-fake" and "--noEmit" in argv
        file = argv[-1].replace("\\", "/")
        out = (
            f"{file}(3,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
            f"{file}(1,1): error TS2307: Cannot find module 'react' or its type declarations.\n"
            f"{file}(2,1): error TS2307: Cannot find module './gone' or its type declarations.\n"
        )
        return CommandResult(2, out)

    report = StaticChecker(temp_parent=str(tmp_path), tsc_path="tsc-fake", runner=fake_tsc).run(
        {"web/a.ts": "const a: number = 'x'\n"}
    )
    assert [(f.tool, f.file, f.line, f.code) for f in report.findings] == [
        ("tsc", "web/a.ts", 2, "TS2307"),
        ("tsc", "web/a.ts", 3, "TS2322"),
    ]
    assert report.tools["tsc"] == "ok" and report.tools["ruff"] == "skipped_no_files"


def test_findings_are_capped(tmp_path: Path) -> None:
    code = "".join(f"import m{i}\n" for i in range(80))
    report = StaticChecker(temp_parent=str(tmp_path)).run({"a.py": code})
    assert len(report.findings) == 50 and report.total >= 80 and report.truncated
    assert all(len(f.message) <= 301 for f in report.findings)


def test_generated_code_is_never_executed_or_imported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, world: World
) -> None:
    marker = tmp_path / "marker.txt"
    evil = (
        "import os\n"
        f"open({str(marker)!r}, 'w').write('executed')\n"
        "os.environ['ARCHPILOT_PWNED'] = '1'\n"
        "raise SystemExit(7)\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARCHPILOT_PWNED", raising=False)
    modules_before = set(sys.modules)
    run, _ = play(
        client,
        world,
        [
            write("src/evil_mod_xyz.py", evil),
            write("ast.py", "raise SystemExit(8)\n"),
            write("mypy.py", "raise SystemExit(9)\n"),
            write("src/conftest.py", "raise SystemExit(10)\n"),
            write("setup.py", "raise SystemExit(11)\n"),
            write("src/test_evil.py", "def test_x():\n    raise SystemExit(12)\n"),
            action("run_static_checks"),
            finish(),
        ],
    )
    assert run["status"] == "finished"
    assert not marker.exists() and "ARCHPILOT_PWNED" not in os.environ
    assert not {m for m in set(sys.modules) - modules_before if "evil_mod" in m}
    assert "evil_mod_xyz" not in sys.modules
    assert (tmp_path / "src").exists() is False  # nothing was written to the working directory


# ---- units -------------------------------------------------------------------------------------


def test_parse_action_edge_cases() -> None:
    ok = parse_action(
        '```json\n{"action": "list_files", "thought": "look"}\n```',
        finish_reason="stop",
        max_chars=500,
    )
    assert ok.name == "list_files"
    with pytest.raises(ActionError) as truncated:
        parse_action("{", finish_reason="length", max_chars=500)
    assert truncated.value.code == "output_truncated"
    with pytest.raises(ActionError) as big:
        parse_action("x" * 600, finish_reason="stop", max_chars=500)
    assert big.value.code == "output_too_large"
    with pytest.raises(ActionError) as extra:
        parse_action(
            '{"action": "finish", "args": {}, "bonus": 1}', finish_reason="stop", max_chars=500
        )
    assert extra.value.code == "invalid_shape"
    secret = "hunter2-do-not-echo"
    with pytest.raises(ActionError) as echo:
        parse_action(
            json.dumps({"action": "write_file", "args": {"path": secret, "content": 5}}),
            finish_reason="stop",
            max_chars=500,
        )
    assert secret not in str(echo.value)


def test_draft_changes() -> None:
    draft = Draft(
        [
            DraftFile("a.py", "1", content_hash("1")),
            DraftFile("b.py", "2", content_hash("2")),
            DraftFile("c.py", "3", content_hash("3")),
        ]
    )
    draft.put("a.py", "changed")
    draft.delete("b.py")
    draft.put("new.py", "n")
    draft.put("tmp.py", "t")
    draft.delete("tmp.py")  # created and removed in the run: no change at all
    assert {(c.path, c.change) for c in draft.changes()} == {
        ("a.py", "modify"),
        ("b.py", "delete"),
        ("new.py", "create"),
    }
    assert draft.get("b.py") is None and draft.clash("NEW.py") == "new.py"


def test_migration_and_models_are_registered(engine: Engine) -> None:
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    assert {"agent_runs", "agent_steps", "agent_draft_files"} <= tables
    with Session(engine) as session:
        assert session.scalars(select(CodePatch)).all() == []


# ---- diagnosis: correct code, environment findings, actionable filter, convergence ---------------

CORRECT_MODULE = """from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()


router = APIRouter()


def dependency() -> int:
    return 1


@router.get("/items")
def names(value: int = Depends(dependency)) -> list[str]:
    return [str(select(Item.name)), str(value)]
"""


def test_correct_file_with_third_party_imports_has_no_findings(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def spy(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        calls.append((argv, cwd))
        return run_command(argv, cwd, env, timeout)

    report = StaticChecker(temp_parent=str(tmp_path), runner=spy).run(
        {"app/models.py": CORRECT_MODULE}
    )
    assert report.findings == [] and report.total == 0 and report.environment == 0
    assert report.tools["ruff"] == "ok" and report.tools["mypy"] == "ok"
    ruff, mypy = (c[0] for c in calls)
    assert "--isolated" in ruff and ruff[ruff.index("--select") + 1] == "E9,F"
    assert "--no-site-packages" in mypy and "--config-file" in mypy
    assert all(cwd.name == "ctl" for _, cwd in calls)  # never the draft or the repository
    assert "ignore_missing_imports = True" in MYPY_CONFIG
    assert list(tmp_path.iterdir()) == []


def test_missing_modules_are_environment_not_code(tmp_path: Path) -> None:
    draft = {
        "app/models.py": "from app.core import settings\nfrom app.db import Base\n\n"
        "VALUE: int = settings.x\n\n\nclass Item(Base):\n    pass\n",
        "app/core.py": "y = 1\n",
    }
    report = StaticChecker(temp_parent=str(tmp_path)).run(draft)
    assert report.findings == [] and report.environment >= 1
    assert report.as_json()["not_shown"]["environment"] == report.environment
    assert any(rule.startswith("mypy:") for rule in report.by_rule)
    kinds = {
        Finding("mypy", "a.py", 1, "import-not-found", "x").category,
        Finding("mypy", "a.py", 1, "import-untyped", "x").category,
        Finding("mypy", "a.py", 1, "attr-defined", 'Module "m" has no attribute "x"').category,
    }
    assert kinds == {"environment"}
    assert Finding("mypy", "a.py", 1, "return-value", "bad").category == "code"
    assert Finding("mypy", "a.py", 1, "attr-defined", '"A" has no attribute "x"').category == "code"
    assert Finding("ruff", "a.py", 1, "F821", "Undefined name").category == "code"


def test_sqlalchemy_base_noise_is_environment_with_reasons(tmp_path: Path) -> None:
    draft = {
        "app/models/base.py": "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n",
        "app/models/user.py": "from app.models.base import Base\n\n\n"
        "class User(Base):\n    pass\n\n\ndef make(u: Base) -> None:\n    return None\n",
    }

    def real_run_mypy(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        if "mypy" not in argv:
            return run_command(argv, cwd, env, timeout)
        work = next(a for a in argv if a.endswith("user.py")).rsplit("app", 1)[0].replace("\\", "/")
        out = (
            f'{work}app/models/user.py:4:12: error: Invalid base class "Base"  [misc]\n'
            f'{work}app/models/user.py:8:12: error: Variable "app.models.base.Base" is not valid '
            "as a type  [valid-type]\n"
        )
        return CommandResult(1, out)

    report = StaticChecker(temp_parent=str(tmp_path), runner=real_run_mypy).run(draft)
    assert report.total == 0 and report.findings == []
    assert report.environment == 2
    reasons = report.as_json()["not_shown"]["reasons"]
    assert len(reasons) == 1 and all("check environment" in r for r in reasons)
    # The same messages for a name defined in the draft stay actionable.
    local = {"m.py": "Base = 1\n\n\nclass User(Base):\n    pass\n"}

    def local_mypy(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        if "mypy" not in argv:
            return run_command(argv, cwd, env, timeout)
        work = next(a for a in argv if a.endswith("m.py")).rsplit("m.py", 1)[0].replace("\\", "/")
        return CommandResult(1, f'{work}m.py:4:12: error: Invalid base class "Base"  [misc]\n')

    assert StaticChecker(temp_parent=str(tmp_path), runner=local_mypy).run(local).total == 1


def test_real_errors_stay_actionable_next_to_environment_noise(tmp_path: Path) -> None:
    base = "from sqlalchemy.orm import declarative_base\n\nBase = declarative_base()\n"
    undefined = StaticChecker(temp_parent=str(tmp_path)).run(
        {"m.py": base + "\n\ndef f() -> int:\n    return nope\n"}
    )
    assert ("m.py", "F821") in {(f.file, f.code) for f in undefined.findings}
    syntax = StaticChecker(temp_parent=str(tmp_path)).run({"m.py": base, "s.py": "def g(:\n"})
    assert ("s.py", "invalid-syntax") in {(f.file, f.code) for f in syntax.findings}


def test_only_actionable_findings_are_returned_with_counts_by_rule(tmp_path: Path) -> None:
    draft = {
        "a.py": "import os\n\n\ndef f() -> int:\n    return missing_name\n",
        "c.py": "from fastapi import Depends\n\n\ndef h(x: int = Depends(int)) -> int:\n    return x\n",  # noqa: E501
        "d.py": "def k() -> int:\n    return 'a'\n",
    }
    report = StaticChecker(temp_parent=str(tmp_path)).run(draft)
    codes_found = {(f.file, f.code) for f in report.findings}
    assert ("a.py", "F401") in codes_found and ("a.py", "F821") in codes_found
    assert ("d.py", "return-value") in codes_found
    assert not any(f.file == "c.py" for f in report.findings)  # B008 style noise is not reported
    assert report.by_rule["ruff:F401"] == 1 and report.by_rule["mypy:return-value"] == 1
    assert report.total == len(report.findings)
    broken = StaticChecker(temp_parent=str(tmp_path)).run({"b.py": "def g(:\n"})
    assert ("b.py", "invalid-syntax") in {(f.file, f.code) for f in broken.findings}


class ScriptedChecker:
    """Reports a scripted number of actionable findings per call; never runs a tool."""

    def __init__(self, totals: list[int]) -> None:
        self.totals = list(totals)
        self.calls = 0

    def run(self, files: Any) -> CheckReport:
        total = self.totals[min(self.calls, len(self.totals) - 1)]
        self.calls += 1
        found = [Finding("ruff", "src/m.py", n + 1, "F401", f"unused {n}") for n in range(total)]
        return CheckReport(found, total, {"ruff": "ok"}, {"ruff:F401": total})


def test_not_converging_stops_early_and_puts_the_findings_in_the_patch_notes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    checker = ScriptedChecker([12, 10, 11, 11, 10, 3])
    configure(monkeypatch, checker=checker)
    run, provider = play(
        client,
        world,
        [write("src/m.py"), *[action("run_static_checks")] * 6, finish()],
    )
    assert (run["status"], run["stop_reason"]) == ("stopped", "not_converging")
    assert checker.calls == 5 and run["steps_taken"] == 6 and len(provider.requests) == 6
    assert run["patch_id"]
    patch = client.get(api(world[0], f"/patches/{run['patch_id']}")).json()
    notes = " ".join(patch["notes"])
    assert "Not converging" in notes and "12, 10, 11, 11, 10" in notes
    assert "src/m.py:1 ruff F401 unused 0" in patch["notes"]
    assert patch["status"] == "proposed"


def test_a_decreasing_count_keeps_the_run_going(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    configure(monkeypatch, checker=ScriptedChecker([5, 4, 3, 2, 1, 0]))
    run, _ = play(client, world, [write("src/m.py"), *[action("run_static_checks")] * 6, finish()])
    assert (run["status"], run["stop_reason"]) == ("finished", "finished")


def test_not_converging_with_an_invalid_draft_stops_without_a_patch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    configure(monkeypatch, checker=ScriptedChecker([4, 4, 4, 4]))
    run, _ = play(
        client, world, [write("src/m.py", "def f(:\n"), *[action("run_static_checks")] * 4]
    )
    assert run["stop_reason"] == "not_converging" and run["patch_id"] is None


def test_static_check_steps_expose_findings_for_the_run_panel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    configure(monkeypatch, checker=ScriptedChecker([2]))
    run, _ = play(client, world, [write("src/m.py"), action("run_static_checks"), finish()])
    step = run["steps"][1]
    assert step["finding_total"] == 2
    assert step["findings"][0] == {
        "tool": "ruff",
        "file": "src/m.py",
        "line": 1,
        "code": "F401",
        "message": "unused 0",
    }
    assert run["steps"][0]["findings"] == [] and run["steps"][0]["finding_total"] is None


# ---- failing loudly: unavailable checkers, health check, syntax errors first --------------------


def hide_tool(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real = importlib.util.find_spec

    def find_spec(module: str, *args: Any) -> Any:
        return None if module == name else real(module, *args)

    monkeypatch.setattr("app.services.static_checks.importlib.util.find_spec", find_spec)


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_a_missing_checker_is_unavailable_never_zero_findings(
    tool: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hide_tool(monkeypatch, tool)
    report = StaticChecker(temp_parent=str(tmp_path)).run({"a.py": "x = 1\n"})
    assert report.tools[tool] == "unavailable" and report.unavailable == [tool]
    assert report.findings == []  # and the caller must not read that as "clean"


def test_a_checker_that_fails_to_start_is_unavailable(tmp_path: Path) -> None:
    def dead(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        return CommandResult(None, "")

    report = StaticChecker(temp_parent=str(tmp_path), runner=dead).run({"a.py": "x = 1\n"})
    assert report.unavailable == ["ruff", "mypy"]


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_run_stops_with_checks_unavailable(
    tool: str, client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    hide_tool(monkeypatch, tool)
    configure(monkeypatch, checker=StaticChecker())
    run, provider = play(client, world, [write("src/m.py"), action("run_static_checks"), finish()])
    assert (run["status"], run["stop_reason"]) == ("stopped", "checks_unavailable")
    assert run["patch_id"] is None and len(provider.requests) == 2
    step = run["steps"][1]
    assert step["status"] == "error" and step["error_code"] == "checks_unavailable"
    assert tool in step["result_summary"]
    stored = json.loads(client.get(api(world[0], f"/agent-runs/{run['id']}")).text)
    assert stored["stop_reason"] == "checks_unavailable"


def test_checks_status_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    body = client.get("/api/agent/checks/status").json()
    assert body["ok"] is True
    for name in ("ruff", "mypy"):
        assert body["tools"][name]["available"] is True
        assert re.search(r"\d+\.\d+", body["tools"][name]["version"])
    hide_tool(monkeypatch, "mypy")
    body = client.get("/api/agent/checks/status").json()
    assert body["ok"] is False
    assert body["tools"]["mypy"] == {"available": False, "version": None}
    assert body["tools"]["ruff"]["available"] is True


def test_syntax_errors_come_first_and_skip_mypy(tmp_path: Path) -> None:
    calls: list[str] = []

    def spy(argv: list[str], cwd: Path, env: Any, timeout: float) -> CommandResult:
        calls.append(argv[argv.index("-m") + 1])
        return run_command(argv, cwd, env, timeout)

    draft = {
        "a.py": "import os\n\n\ndef f() -> int:\n    return 'a'\n",
        "b.py": "def g(:\n",
    }
    report = StaticChecker(temp_parent=str(tmp_path), runner=spy).run(draft)
    assert report.findings and all(f.file == "b.py" for f in report.findings)
    assert {f.code for f in report.findings} <= {"invalid-syntax", "python_syntax"}
    assert calls == ["ruff"] and report.tools["mypy"] == "skipped_syntax_errors"
    assert report.as_json()["notes"] == [SYNTAX_ONLY_NOTE]
    fixed = StaticChecker(temp_parent=str(tmp_path), runner=spy).run({**draft, "b.py": "g = 1\n"})
    assert {f.code for f in fixed.findings} == {"F401", "return-value"} and fixed.notes == []


def test_a_clean_file_still_returns_zero_findings(tmp_path: Path) -> None:
    report = StaticChecker(temp_parent=str(tmp_path)).run({"a.py": "x: int = 1\n"})
    assert report.findings == [] and report.total == 0 and report.unavailable == []
    assert report.tools["ruff"] == "ok" and report.tools["mypy"] == "ok"
