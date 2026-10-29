"""Execution provider interface (Phase 11B-prep). Only fakes run here; nothing is executed."""

import ast
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.execution import (
    COMMANDS,
    ELSEWHERE,
    IN_CHANGED,
    ExecutionResult,
    FakeExecutionProvider,
    ProviderUnavailableError,
    UnavailableExecutionProvider,
    UnknownCommandError,
    attribute,
    check_command,
    check_select,
    compare_baseline,
    make_result,
    parse_junit,
)
from app.services.execution import TestRunner, cancel_run_jobs, open_jobs, release_run
from tests.test_agent import (
    action,
    configure,
    finish,
    run_of,
    scripted,
    start,
    write,
)
from tests.test_agent import clean_agent_state as _clean_agent_state
from tests.test_agent import world as _agent_world
from tests.test_patches import World, api

world = _agent_world
clean_agent_state = _clean_agent_state  # autouse: runs the agent inline


def junit(*cases: tuple[str, str, str]) -> str:
    """JUnit XML as pytest writes it. A case is (classname, name, "" | "failure" | "error" |
    "skipped")."""
    body = ""
    for classname, name, kind in cases:
        inner = f'<{kind} message="boom">trace</{kind}>' if kind else ""
        body += f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'
    return f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>'


def result(report: str | None = None, code: int = 0, **extra: Any) -> ExecutionResult:
    return ExecutionResult(code, report=report, **extra)


A_OK = ("tests.test_a", "test_one", "")
A_FAIL = ("tests.test_a", "test_one", "failure")
B_FAIL = ("tests.test_b", "test_two", "failure")
B_OK = ("tests.test_b", "test_two", "")


def runner(provider: FakeExecutionProvider, timeout: float = 5.0, cap: int = 4000) -> TestRunner:
    return TestRunner(provider, timeout=timeout, output_cap=cap)


# ---- allowlist ----------------------------------------------------------------------------------


def test_allowlist_is_fixed_ids_never_free_text() -> None:
    assert set(COMMANDS) == {"pytest", "ruff", "mypy", "tsc", "npm test"}
    for bad in ["rm -rf /", "pytest; rm x", "pytest -x", "", "PYTEST", "python -c 'x'"]:
        with pytest.raises(UnknownCommandError):
            check_command(bad)
    with pytest.raises(UnknownCommandError):
        check_select("--collect-only")


def test_runner_and_providers_refuse_commands_off_the_list() -> None:
    fake = FakeExecutionProvider()
    with pytest.raises(UnknownCommandError):
        runner(fake).run(uuid.uuid4(), "make all", {"a.py": ""}, None, [])
    with pytest.raises(UnknownCommandError):
        fake.run("make all", {}, 1, 10)
    with pytest.raises(UnknownCommandError):
        UnavailableExecutionProvider().run("make all", {}, 1, 10)
    assert fake.calls == []


# ---- truncation, timeout, cancel ---------------------------------------------------------------


def test_output_is_capped_for_the_model_and_kept_in_full_in_the_log() -> None:
    big = "x" * 10_000
    fake = FakeExecutionProvider(
        {"ruff": [make_result(exit_code=1, stdout=big, output_cap=10_000)]}
    )
    outcome = runner(fake, cap=100).run(uuid.uuid4(), "ruff", {"a.py": ""}, None, ["a.py"])
    assert len(outcome.payload["stdout_tail"]) == 100 and outcome.payload["truncated"] is True
    assert outcome.jobs[0].output == big and outcome.jobs[0].truncated is True
    cut = make_result(exit_code=0, stdout="abcdef", output_cap=3)
    assert (cut.stdout_tail, cut.truncated, cut.log) == ("def", True, "abcdef")


def test_timeout_is_a_status_not_a_pass() -> None:
    fake = FakeExecutionProvider({"pytest": [FakeExecutionProvider.HANG]})
    started = time.monotonic()
    outcome = runner(fake, timeout=0.1).run(uuid.uuid4(), "pytest", {"a.py": ""}, None, [])
    assert time.monotonic() - started < 3
    assert outcome.payload["status"] == "timed_out" and outcome.payload["exit_code"] is None
    assert [j.status for j in outcome.jobs] == ["timed_out"] and outcome.jobs[0].timeout == 0.1


def test_cancel_stops_an_open_job_and_refuses_new_ones() -> None:
    fake = FakeExecutionProvider({"pytest": [FakeExecutionProvider.HANG]})
    run_id = uuid.uuid4()
    box: dict[str, Any] = {}

    def work() -> None:
        box["outcome"] = runner(fake, timeout=30).run(run_id, "pytest", {"a.py": ""}, None, [])

    thread = threading.Thread(target=work)
    thread.start()
    for _ in range(100):
        if open_jobs(run_id):
            break
        time.sleep(0.02)
    assert len(open_jobs(run_id)) == 1
    assert cancel_run_jobs(run_id) == 1
    thread.join(5)
    assert not thread.is_alive()
    assert box["outcome"].payload["status"] == "cancelled"
    assert box["outcome"].jobs[0].status == "cancelled" and open_jobs(run_id) == []
    later = runner(fake).run(run_id, "pytest", {"a.py": ""}, None, [])
    assert later.payload["status"] == "cancelled"  # a stopped run starts no new job
    release_run(run_id)


def test_stopping_a_run_cancels_its_open_jobs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    monkeypatch.setattr("app.api.agent._spawn", lambda job: None)
    cancelled: list[uuid.UUID] = []
    monkeypatch.setattr("app.services.agent.cancel_run_jobs", cancelled.append)
    scripted([finish()])
    run_id = start(client, world).json()["id"]
    client.post(api(world[0], f"/agent-runs/{run_id}/stop"))
    assert cancelled == [uuid.UUID(run_id)]


# ---- structured parsing -------------------------------------------------------------------------


def test_junit_is_parsed_into_structured_cases() -> None:
    xml = (
        "<testsuites><testsuite>"
        '<testcase classname="tests.test_a.TestX" name="test_ok" file="tests/test_a.py" line="7"/>'
        '<testcase classname="tests.test_b" name="test_bad"><failure message="assert 1 == 2">'
        "tb</failure></testcase>"
        '<testcase classname="tests.test_c" name="test_err"><error message="ImportError"/>'
        "</testcase>"
        '<testcase classname="tests.test_d" name="test_skip"><skipped message="why"/></testcase>'
        "</testsuite></testsuites>"
    )
    cases = {c.name: c for c in parse_junit(xml)}
    ok = cases["tests.test_a.TestX::test_ok"]
    assert (ok.outcome, ok.file, ok.line, ok.message) == ("passed", "tests/test_a.py", 7, "")
    bad = cases["tests.test_b::test_bad"]
    assert (bad.outcome, bad.message, bad.file, bad.line) == (
        "failed",
        "assert 1 == 2",
        "tests/test_b.py",
        None,
    )
    assert cases["tests.test_c::test_err"].outcome == "error"
    assert cases["tests.test_d::test_skip"].outcome == "skipped"
    assert set(bad.as_json()) == {"name", "outcome", "message", "file", "line"}


def test_junit_refuses_garbage_and_entity_tricks() -> None:
    for bad in ["not xml", '<!DOCTYPE x [<!ENTITY a "b">]><testsuites/>']:
        with pytest.raises(ValueError):
            parse_junit(bad)


def test_output_text_is_never_scraped_for_test_results() -> None:
    fake = FakeExecutionProvider(
        {"pytest": [result(None, 1, stdout_tail="FAILED tests/test_a.py::test_one - boom")]}
    )
    payload = runner(fake).run(uuid.uuid4(), "pytest", {"a.py": ""}, None, []).payload
    assert payload["structured"] is False and "new" not in payload
    assert payload["status"] == "failed"


# ---- baseline, attribution, flaky --------------------------------------------------------------


def test_baseline_comparison_new_pre_existing_and_fixed() -> None:
    before = parse_junit(junit(A_FAIL, B_FAIL, ("tests.test_c", "test_three", "")))
    after = parse_junit(
        junit(
            ("tests.test_a", "test_one", ""),  # was failing: fixed
            B_FAIL,  # still failing: pre-existing
            ("tests.test_c", "test_three", "failure"),  # was passing: new
        )
    )
    found = compare_baseline(before, after)
    assert [c.name for c in found.new] == ["tests.test_c::test_three"]
    assert [c.name for c in found.pre_existing] == ["tests.test_b::test_two"]
    assert [c.name for c in found.fixed] == ["tests.test_a::test_one"]
    assert [c.name for c in compare_baseline(None, after).new] == [
        "tests.test_b::test_two",
        "tests.test_c::test_three",
    ]


def test_the_model_gets_new_failures_and_a_pre_existing_count() -> None:
    fake = FakeExecutionProvider(
        {
            "pytest": [
                result(junit(A_FAIL, B_FAIL, ("tests.test_c", "test_three", "")), 1),
                result(junit(A_OK, B_FAIL, ("tests.test_c", "test_three", "failure")), 1),
            ]
        }
    )
    fake.add(
        "pytest::tests.test_c::test_three",
        result(junit(("tests.test_c", "test_three", "failure")), 1),
    )
    fake.add("pytest::tests.test_b::test_two", result(junit(B_FAIL), 1))
    outcome = runner(fake).run(
        uuid.uuid4(),
        "pytest",
        {"tests/test_c.py": "changed"},
        {"tests/test_c.py": "base", "tests/test_b.py": ""},
        ["tests/test_c.py"],
    )
    p = outcome.payload
    assert [n["name"] for n in p["new"]] == ["tests.test_c::test_three"]
    assert (p["pre_existing"], p["fixed"], p["new_in_changed"], p["new_elsewhere"]) == (1, 1, 1, 0)
    assert "tests.test_b::test_two" not in json.dumps(p["new"])
    first, second = fake.calls[0], fake.calls[1]
    assert first.paths == ["tests/test_b.py", "tests/test_c.py"] and second.paths == [
        "tests/test_c.py"
    ]
    assert [j.variant for j in outcome.jobs[:2]] == ["baseline", "changed"]


def test_findings_are_tagged_in_changed_files_or_elsewhere() -> None:
    tagged, counts = attribute(
        [{"file": "src/a.py", "line": 1}, {"file": "src/old.py", "line": 2}, {"file": None}],
        ["src/a.py"],
    )
    assert [t["scope"] for t in tagged] == [IN_CHANGED, ELSEWHERE, ELSEWHERE]
    assert counts == {IN_CHANGED: 1, ELSEWHERE: 2}


def test_new_failures_elsewhere_are_counted_not_listed() -> None:
    fake = FakeExecutionProvider(
        {
            "pytest": [result(junit(A_FAIL, B_FAIL), 1)],
            "pytest::tests.test_a::test_one": [result(junit(A_FAIL), 1)],
            "pytest::tests.test_b::test_two": [result(junit(B_FAIL), 1)],
        }
    )
    p = (
        runner(fake)
        .run(uuid.uuid4(), "pytest", {"tests/test_a.py": ""}, None, ["tests/test_a.py"])
        .payload
    )
    assert [n["name"] for n in p["new"]] == ["tests.test_a::test_one"]
    assert (p["new_in_changed"], p["new_elsewhere"]) == (1, 1)


def test_a_failed_test_is_rerun_alone_and_a_pass_marks_it_flaky() -> None:
    fake = FakeExecutionProvider(
        {
            "pytest": [result(junit(A_FAIL, B_FAIL), 1)],
            "pytest::tests.test_a::test_one": [result(junit(A_OK), 0)],  # passes alone
            "pytest::tests.test_b::test_two": [result(junit(B_FAIL), 1)],  # fails again
        }
    )
    outcome = runner(fake).run(
        uuid.uuid4(),
        "pytest",
        {"tests/test_a.py": ""},
        None,
        ["tests/test_a.py", "tests/test_b.py"],
    )
    p = outcome.payload
    assert p["flaky"] == ["tests.test_a::test_one"]
    assert [n["name"] for n in p["new"]] == ["tests.test_b::test_two"]
    assert [(c.select) for c in fake.calls[1:]] == [
        "tests.test_a::test_one",
        "tests.test_b::test_two",
    ]
    reruns = [j for j in outcome.jobs if j.variant == "rerun"]
    assert [j.summary["flaky"] for j in reruns] == [True, False]  # recorded in the run log


def test_all_passing_needs_no_rerun() -> None:
    fake = FakeExecutionProvider({"pytest": [result(junit(A_OK, B_OK), 0)]})
    outcome = runner(fake).run(uuid.uuid4(), "pytest", {"a.py": ""}, None, [])
    assert outcome.payload["status"] == "ok" and len(fake.calls) == 1


# ---- unavailable provider ----------------------------------------------------------------------


def test_unavailable_provider_says_so_and_never_runs() -> None:
    provider = UnavailableExecutionProvider()
    ok, reason = provider.status()
    assert ok is False and reason
    with pytest.raises(ProviderUnavailableError, match="tests unavailable"):
        provider.run("pytest", {}, 1, 10)
    with pytest.raises(ProviderUnavailableError):
        TestRunner(provider, timeout=1, output_cap=10).run(uuid.uuid4(), "pytest", {}, None, [])


def test_agent_run_tests_with_no_provider_returns_unavailable_and_notes_it(
    client: TestClient, world: World
) -> None:
    scripted([action("run_tests", command_id="pytest"), write("src/a.py"), finish()])
    run = run_of(client, world, start(client, world))
    step = run["steps"][0]
    assert (step["action"], step["status"], step["error_code"]) == (
        "run_tests",
        "error",
        "tests_unavailable",
    )
    assert step["result_summary"] == "tests unavailable"
    assert run["status"] == "finished" and run["jobs"] == []
    patch = client.get(api(world[0], f"/patches/{run['patch_id']}")).json()
    assert "tests unavailable" in json.dumps(patch)


def test_run_tests_is_offered_only_with_a_real_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    provider = scripted([finish()])
    run_of(client, world, start(client, world))
    assert "run_tests" not in provider.requests[0].prompt.split("BEGIN_CONTEXT_")[1]
    configure(monkeypatch, execution=FakeExecutionProvider())
    provider = scripted([finish()])
    run_of(client, world, start(client, world))
    assert '"run_tests": "available' in provider.requests[0].prompt


# ---- agent action ------------------------------------------------------------------------------


def test_agent_run_tests_action_reports_to_the_model_and_the_panel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    fake = FakeExecutionProvider(
        {
            "pytest": [result(junit(("tests.test_n", "test_new", "failure")), 1)],
            "pytest::tests.test_n::test_new": [
                result(junit(("tests.test_n", "test_new", "failure")), 1)
            ],
        }
    )
    configure(monkeypatch, execution=fake)
    provider = scripted(
        [write("tests/test_n.py"), action("run_tests", command_id="pytest"), finish()]
    )
    run = run_of(client, world, start(client, world))
    step = run["steps"][1]
    assert step["status"] == "ok" and step["tests"]["new_in_changed"] == 1
    assert step["tests"]["new"][0]["file"] == "tests/test_n.py"
    assert {j["variant"] for j in run["jobs"]} == {"changed", "rerun"}
    assert all(j["timeout_seconds"] == 120.0 for j in run["jobs"])
    assert "tests.test_n::test_new" in provider.requests[2].prompt  # the model saw the failure


def test_agent_refuses_a_command_off_the_allowlist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    fake = FakeExecutionProvider()
    configure(monkeypatch, execution=fake)
    scripted([action("run_tests", command_id="rm -rf /"), finish()])
    run = run_of(client, world, start(client, world))
    assert run["steps"][0]["error_code"] == "unknown_command" and fake.calls == []


def test_static_findings_carry_their_scope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    from app.services.static_checks import CheckReport, Finding

    class Checker:
        def run(self, files: dict[str, str]) -> CheckReport:
            found = [
                Finding("ruff", "src/a.py", 1, "F821", "undefined name"),
                Finding("ruff", "src/old.py", 2, "F401", "unused"),
            ]
            return CheckReport(found, len(found), {"ruff": "1", "mypy": "1"})

    configure(monkeypatch, checker=Checker())
    scripted([write("src/a.py", "x = y\n"), action("run_static_checks"), finish()])
    step = run_of(client, world, start(client, world))["steps"][1]
    assert {f["file"]: f["scope"] for f in step["findings"]} == {
        "src/a.py": IN_CHANGED,
        "src/old.py": ELSEWHERE,
    }
    assert step["scope_counts"] == {IN_CHANGED: 1, ELSEWHERE: 1}


# ---- nothing executes locally ------------------------------------------------------------------


def test_execution_modules_import_no_process_or_shell_apis() -> None:
    root = Path(__file__).resolve().parent.parent / "app"
    forbidden = {"subprocess", "os", "pty", "runpy", "importlib", "shlex"}
    for relative in ("domain/execution.py", "services/execution.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & forbidden, relative
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden, relative
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"exec", "eval", "compile", "__import__"}, relative
