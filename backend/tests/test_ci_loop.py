"""Phase 13E: the iterative CI repair loop of one approved checklist step.

Everything remote is mocked: GitHub is an httpx.MockTransport (`LoopGitHub`), the model is the
fake provider, no Git command runs and no remote is contacted."""

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.domain.ci_loop import (
    Batch,
    Convergence,
    ImportMap,
    approval_problem,
    classify,
    failure_signature,
    is_green,
    normalize_message,
    normalize_path,
    protected,
    resume_problems,
)
from app.domain.execution import CaseResult, ProviderUnavailableError, parse_junit
from app.domain.git_remote import parse_remote
from app.llm.fake_provider import FakeLLMProvider
from app.models.agent import AgentJob
from app.repositories.patch import PatchRepository
from app.services import agent as agent_service
from app.services import ci_loop
from app.services.ci_execution import ARTIFACT_NAME, CIError, CITarget, check_ci_branch
from app.services.ci_loop import CHECKPOINT, ITERATION, CILoop, LoopConfig
from app.tools import UnknownToolError, get_registry
from tests.test_agent import ScriptedChecker, action, write
from tests.test_checklists import (
    CRITERIA,
    approved_two_steps,
    body_for,
    run_next,
    steps_in,
    stop_after,
    url,
    view,
)
from tests.test_ci_execution import PAT, REPO, target, wait_for, zipped
from tests.test_patches import World, api
from tests.test_patches import world as _world_fixture
from tests.test_plans import approved_project
from tests.test_requirements_api import use

world = _world_fixture

STORAGE = "https://storage.example.net"
INVOICE = "src/invoice.py"
TEST_FILE = "tests/test_invoice.py"


# ---- JUnit reports -------------------------------------------------------------------------------


def case(
    name: str,
    *,
    cls: str = "tests.test_invoice",
    file: str | None = None,
    fail: str | None = None,
    trace: str = "tb",
    flaky: bool = False,
) -> str:
    attrs = f'classname="{cls}" name="{name}"' + (f' file="{file}"' if file else "")
    body = ""
    if fail is not None:
        body = f'<failure message="{fail}">{trace}</failure>'
    elif flaky:
        body = f'<rerunFailure message="timeout">{trace}</rerunFailure>'
    return f"<testcase {attrs}>{body}</testcase>"


def report(*cases: str) -> str:
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


OK = case("test_fine", cls="tests.test_ok", file="tests/test_ok.py")


# ---- a GitHub with state -------------------------------------------------------------------------


class LoopGitHub:
    """The Git Data and Actions endpoints with real state: refs move, commits chain, every
    dispatch creates a workflow run whose result is scripted by `reports`."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[httpx.Request] = []
        self.default_branch = "main"
        self.refs: dict[str, str] = {"main": "base0"}
        self.commits: dict[str, dict[str, Any]] = {
            "base0": {"tree": "tree0", "message": "base", "parents": []}
        }
        self.trees: list[dict[str, Any]] = []
        # Workflow run n (0: the baseline) gets reports[min(n, len - 1)]: (conclusion, xml).
        self.reports: list[tuple[str, str | None]] = [("success", report(OK))]
        self.runs: dict[int, dict[str, Any]] = {}
        self.hang_from: int | None = None  # workflow runs from this index never complete
        self.pulls: list[dict[str, Any]] = []
        self.fail: dict[tuple[str, str], tuple[int, int]] = {}  # key -> (from call n, status)
        self.calls: dict[tuple[str, str], int] = {}

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=False)

    def body(self, request: httpx.Request) -> Any:
        return json.loads(request.content or b"null")

    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    def sent(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    def dispatches(self) -> list[httpx.Request]:
        return self.sent("POST", "/dispatches")

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method, full = request.method, str(request.url)
        if full.startswith(STORAGE + "/"):
            number = int(request.url.path.strip("/").removesuffix(".zip"))
            return httpx.Response(200, content=zipped({"junit.xml": self.runs[number]["xml"]}))
        path = re.sub(r"^/repos/[^/]+/[^/]+", "", request.url.path)
        key = (method, path)
        self.calls[key] = self.calls.get(key, 0) + 1
        if key in self.fail and self.calls[key] > self.fail[key][0]:
            return httpx.Response(self.fail[key][1], json={"message": f"bad credentials {PAT}"})
        if method == "GET" and path == "":
            return httpx.Response(200, json={"default_branch": self.default_branch})
        if path.startswith("/git/ref/heads/") and method == "GET":
            branch = path.removeprefix("/git/ref/heads/")
            if branch not in self.refs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.refs[branch]}})
        if method == "GET" and path.startswith("/git/commits/"):
            found = self.commits[path.removeprefix("/git/commits/")]
            return httpx.Response(200, json={**found, "tree": {"sha": found["tree"]}})
        if method == "GET" and path.startswith("/git/trees/"):
            return httpx.Response(200, json={"tree": [], "truncated": False})
        if method == "POST" and path == "/git/trees":
            self.trees.append(self.body(request))
            return httpx.Response(201, json={"sha": f"tree{len(self.trees)}"})
        if method == "POST" and path == "/git/commits":
            data = self.body(request)
            sha = f"c{len(self.commits)}"
            self.commits[sha] = {
                "tree": data["tree"],
                "message": data["message"],
                "parents": data["parents"],
            }
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/git/refs":
            data = self.body(request)
            branch = data["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[branch] = data["sha"]
            return httpx.Response(201, json={})
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            self.refs[path.removeprefix("/git/refs/heads/")] = self.body(request)["sha"]
            return httpx.Response(200, json={})
        if method == "DELETE" and path.startswith("/git/refs/heads/"):
            self.refs.pop(path.removeprefix("/git/refs/heads/"), None)
            return httpx.Response(204)
        if method == "POST" and path.endswith("/dispatches"):
            data = self.body(request)
            index = len(self.runs)
            conclusion, xml = self.reports[min(index, len(self.reports) - 1)]
            self.runs[1000 + index] = {
                "head_sha": self.refs[data["ref"]],
                "conclusion": conclusion,
                "xml": xml,
                "hang": self.hang_from is not None and index >= self.hang_from,
                "inputs": data["inputs"],
                "cancelled": False,
            }
            return httpx.Response(204)
        if method == "GET" and path.endswith("/runs") and "/workflows/" in path:
            sha = request.url.params.get("head_sha")
            listed = [
                {"id": n, "head_sha": r["head_sha"]}
                for n, r in sorted(self.runs.items(), reverse=True)
                if r["head_sha"] == sha
            ]
            return httpx.Response(200, json={"workflow_runs": listed})
        matched = re.fullmatch(r"/actions/runs/(\d+)(/cancel|/artifacts)?", path)
        if matched:
            number, tail = int(matched.group(1)), matched.group(2)
            found = self.runs[number]
            if tail == "/cancel" and method == "POST":
                found["cancelled"] = True
                return httpx.Response(202, json={})
            if tail == "/artifacts":
                items = []
                if found["xml"] is not None:
                    items.append(
                        {
                            "name": ARTIFACT_NAME,
                            "expired": False,
                            "size_in_bytes": len(found["xml"]),
                            "archive_download_url": f"{REPO}/actions/artifacts/{number}/zip",
                        }
                    )
                return httpx.Response(200, json={"artifacts": items})
            if found["hang"]:
                return httpx.Response(200, json={"status": "in_progress", "conclusion": None})
            return httpx.Response(
                200, json={"status": "completed", "conclusion": found["conclusion"]}
            )
        matched = re.fullmatch(r"/actions/artifacts/(\d+)/zip", path)
        if matched:
            return httpx.Response(302, headers={"location": f"{STORAGE}/{matched.group(1)}.zip"})
        if method == "GET" and path == "/pulls":
            return httpx.Response(200, json=self.pulls)
        return httpx.Response(599, json={"message": f"unexpected {method} {path}"})


# ---- the harness ---------------------------------------------------------------------------------


class Harness:
    def __init__(self) -> None:
        self.gh = LoopGitHub()
        self.checker = ScriptedChecker([0])
        self.config = LoopConfig(
            poll=0.01, ci_timeout=20, approval_timeout=10, max_iterations=5, max_edits=5
        )
        self.targets: dict[str, CITarget] = {}
        self.threads: list[threading.Thread] = []

    def target_of(self, project_id: uuid.UUID) -> CITarget:
        found = self.targets.get(str(project_id))
        if found is None:
            raise ProviderUnavailableError("No repository is connected to this project")
        return found

    def loop(self, session: Session) -> CILoop:
        patches = PatchRepository(session)
        return CILoop(
            self.target_of,
            lambda pid: {f.path: f.content for f in patches.workspace(pid)},
            self.config,
            client=self.gh.client(),
        )

    def spawn(self, job: Callable[[], None]) -> None:
        thread = threading.Thread(target=job, daemon=True)
        self.threads.append(thread)
        thread.start()

    def join(self, seconds: float = 20) -> None:
        for thread in self.threads:
            thread.join(seconds)
        assert not any(t.is_alive() for t in self.threads), "the agent run did not end"


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch, world: World) -> Iterator[Harness]:
    harness = Harness()
    harness.targets[world[0]] = target()
    monkeypatch.setattr("app.api.agent._spawn", harness.spawn)
    monkeypatch.setattr("app.api.deps.make_ci_loop", harness.loop)
    real = deps.make_agent_service
    monkeypatch.setattr(
        "app.api.checklists.make_agent_service",
        lambda s, p: real(s, p, checker=harness.checker),  # type: ignore[arg-type]
    )
    yield harness
    for run_id in list(ci_loop._live):
        ci_loop.cancel_loop(run_id)
    for thread in harness.threads:
        thread.join(10)
    for run_id in list(ci_loop._live):
        ci_loop.release_loop(run_id)
    agent_service._active.clear()
    if agent_service._agent_lock.locked():
        agent_service._agent_lock.release()


def settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    patched = get_settings().model_copy(update={f"agent_{k}": v for k, v in values.items()})
    monkeypatch.setattr("app.api.deps.get_settings", lambda: patched)


def model(replies: list[str]) -> FakeLLMProvider:
    provider = FakeLLMProvider(agent_script=list(replies))
    use(provider)
    return provider


def start(client: TestClient, world: World, **body: Any) -> str:
    response = run_next(client, world, **body)
    assert response.status_code == 202, response.text
    run_id: str = response.json()["id"]
    return run_id


def run_url(world: World, run_id: str, suffix: str = "") -> str:
    return api(world[0], f"/agent-runs/{run_id}{suffix}")


def get_run(client: TestClient, world: World, run_id: str) -> dict[str, Any]:
    response = client.get(run_url(world, run_id))
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def loop_of(client: TestClient, world: World, run_id: str) -> dict[str, Any]:
    found: dict[str, Any] = get_run(client, world, run_id).get("ci_loop") or {}
    return found


def await_pending(client: TestClient, world: World, run_id: str) -> None:
    wait_for(lambda: loop_of(client, world, run_id).get("approval") == "pending", 15)


def approve(client: TestClient, world: World, run_id: str, by: str = "Ivan") -> dict[str, Any]:
    await_pending(client, world, run_id)
    response = client.post(run_url(world, run_id, "/ci-loop/approval"), json={"approved_by": by})
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def finished(client: TestClient, world: World, h: Harness, run_id: str) -> dict[str, Any]:
    h.join()
    return get_run(client, world, run_id)


def loop_rows(engine: Engine, run_id: str, variant: str) -> list[dict[str, Any]]:
    with Session(engine) as session:
        found = session.scalars(
            select(AgentJob).where(
                AgentJob.run_id == uuid.UUID(run_id), AgentJob.variant == variant
            )
        ).all()
        return sorted((dict(r.summary or {}) for r in found), key=lambda s: int(s.get("seq") or 0))


def invoice(extra: str = "") -> str:
    return body_for(CRITERIA[0]) + extra


def extend(extra: str) -> str:
    """A later edit of the invoice the model already wrote (write_file refuses a rewrite)."""
    return action("edit_file", path=INVOICE, search=invoice(), replace=invoice(extra))


def patch_of(client: TestClient, world: World, run: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = client.get(api(world[0], f"/patches/{run['patch_id']}")).json()
    return found


# ---- pure parts ----------------------------------------------------------------------------------


def test_failure_signature_drops_volatile_ids_durations_lines_and_workspace_paths() -> None:
    one = CaseResult(
        "tests.test_a::test_x",
        "failed",
        "AssertionError: object at 0x7f3a2b1c id=4411 took 0.52s "
        "(run 8812) 1b4e28ba-2fa1-11d2-883f-0016d3cca427 line 12",
        "/home/runner/work/app/app/tests/test_a.py",
        12,
    )
    two = CaseResult(
        "tests.test_a::test_x",
        "failed",
        "AssertionError: object at 0x99aa00ff id=7 took 31 ms "
        "(run 9) 00000000-0000-4000-8000-000000000000 line 98",
        "tests\\test_a.py",
        98,
    )
    assert normalize_path(one.file) == normalize_path(two.file) == "tests/test_a.py"
    assert normalize_message(one.message) == normalize_message(two.message)
    assert failure_signature(one) == failure_signature(two)
    other_test = CaseResult(**{**one.__dict__, "name": "tests.test_a::test_y"})
    other_error = CaseResult(**{**one.__dict__, "message": "KeyError: 'paid'"})
    assert len({failure_signature(c) for c in (one, other_test, other_error)}) == 3
    assert normalize_path("D:/a/app/app/src/x.py") == "src/x.py"
    assert normalize_path("./src/x.py") == "src/x.py"


def test_parse_junit_marks_rerun_cases_as_flaky_and_keeps_the_trace() -> None:
    xml = report(
        case("test_retry", flaky=True, trace="src/net.py:3 timeout"),
        case("test_bad", fail="AssertionError: no", trace="File src/invoice.py, line 4"),
    )
    retry, bad = parse_junit(xml)
    assert (retry.outcome, retry.flaky) == ("passed", True)
    assert (bad.outcome, bad.flaky) == ("failed", False)
    assert "src/invoice.py" in bad.trace and "trace" not in bad.as_json()


def test_classification_keeps_novelty_and_file_attribution_apart() -> None:
    files = {
        INVOICE: "PAID = True\n",
        "src/ledger.py": "from src.invoice import PAID\n",
        "tests/test_ledger.py": "from src import ledger\n",
        "web/src/api.ts": "export const x = 1\n",
        "web/src/view.test.ts": "import { x } from './api'\n",
    }
    cases = parse_junit(
        report(
            OK,
            case("test_old", cls="tests.test_legacy", file="tests/test_legacy.py", fail="E"),
            case("test_was_broken", cls="tests.test_fixme", file="tests/test_fixme.py"),
            case("test_paid", file=TEST_FILE, fail="AssertionError: not paid"),
            case("test_trace", cls="tests.test_report", file="tests/test_report.py",
                 fail="KeyError: k", trace=f"Traceback\n  File /home/runner/work/a/a/{INVOICE}"),
            case("test_ledger", cls="tests.test_ledger", file="tests/test_ledger.py",
                 fail="ImportError: x"),
            case("test_view", cls="web.view", file="web/src/view.test.ts", fail="Error: y"),
            case("test_unrelated", cls="tests.test_other", file="tests/test_other.py",
                 fail="ValueError: z"),
            case("test_retry", cls="tests.test_net", file="tests/test_net.py", flaky=True),
        )
    )  # fmt: skip
    result = classify(
        cases,
        baseline_failing=["tests.test_legacy::test_old", "tests.test_fixme::test_was_broken"],
        changed_paths=[INVOICE, TEST_FILE, "web/src/api.ts"],
        imports=ImportMap(files),
    )
    by = {r.name.split("::")[1]: r for r in result.results}
    assert by["test_old"].novelty == "pre_existing"
    assert by["test_was_broken"].novelty == "fixed"
    assert by["test_retry"].novelty == "flaky"
    assert (by["test_paid"].novelty, by["test_paid"].scope) == ("new", "changed")
    assert by["test_trace"].scope == "elsewhere" and by["test_trace"].linked_via == INVOICE
    # An unchanged test reaches a changed file through its imports (Python and TypeScript).
    assert by["test_ledger"].linked_via == INVOICE
    assert by["test_view"].linked_via == "web/src/api.ts"
    assert by["test_unrelated"].linked_via is None and "test_fine" not in by
    assert result.counts == {
        "new": 5, "pre_existing": 1, "fixed": 1, "flaky": 1, "elsewhere": 4,
    }  # fmt: skip
    feed = result.model_feed()
    names = [f["test"].split("::")[1] for f in feed["actionable_failures"]]
    assert names == ["test_paid", "test_trace", "test_ledger", "test_view"]
    assert feed["flaky"] == [{"test": "tests.test_net::test_retry", "file": "tests/test_net.py"}]
    assert feed["counts"] == {"pre_existing": 1, "elsewhere": 4, "fixed": 1}
    text = json.dumps(feed)
    assert "test_unrelated" not in text and "test_old" not in text  # shown to the reviewer only
    assert [r.name for r in result.unlinked_elsewhere] == ["tests.test_other::test_unrelated"]
    assert not is_green(result, "failure", True)


def test_green_tolerates_pre_existing_and_flaky_failures_only() -> None:
    cases = parse_junit(report(case("test_old", fail="E"), case("test_retry", flaky=True), OK))
    tolerated = classify(cases, baseline_failing=["tests.test_invoice::test_old"], changed_paths=[])
    assert is_green(tolerated, "failure", True)
    assert not is_green(tolerated, "cancelled", True)
    assert is_green(classify([], baseline_failing=[], changed_paths=[]), "success", False)
    assert not is_green(classify([], baseline_failing=[], changed_paths=[]), "failure", False)


def test_convergence_needs_three_distinct_workflow_runs_without_improvement() -> None:
    found = Convergence()
    assert found.add(1, ["a", "b"], 2)
    assert not found.add(1, ["a", "b"], 2)  # the same workflow run again: not an iteration
    assert found.add(2, ["a"], 1)
    assert found.add(3, ["a"], 1) and not found.not_converging()  # 2 -> 1 is an improvement
    assert found.add(4, ["a"], 1) and found.not_converging()
    assert Convergence.from_json(found.as_json()).history == found.history
    changing = Convergence()
    for n, sig in enumerate(["a", "b", "c"]):
        changing.add(n, [sig], 1)
    assert not changing.not_converging()


def test_edits_are_batched_until_static_clean_or_the_batch_is_full() -> None:
    batch = Batch(3)
    assert batch.after("read_file", True, None) is None
    assert batch.after("write_file", False, 0) is None  # a failed edit is no edit
    assert batch.after("write_file", True, 2) is None
    assert batch.after("edit_file", True, None) is None
    assert batch.after("delete_file", True, 1) == "batch_full"
    batch.reset()
    assert batch.after("edit_file", True, 0) == "static_clean"


def test_base_default_main_and_master_are_never_approved_or_pushed() -> None:
    record = {
        "project_id": "p",
        "run_id": "r",
        "branch": "agent/ci/r",
        "revoked_at": None,
    }
    common = {"project_id": "p", "run_id": "r", "base_branch": "develop", "default_branch": "trunk"}
    assert approval_problem(record, branch="agent/ci/r", **common) is None
    for branch in ("main", "MASTER", "develop", "trunk"):
        assert protected(branch, base_branch="develop", default_branch="trunk")
        assert "base, default, main or master" in str(
            approval_problem({**record, "branch": branch}, branch=branch, **common)
        )
    assert approval_problem(None, branch="agent/ci/r", **common) == "no approval"
    assert approval_problem({**record, "revoked_at": "now"}, branch="agent/ci/r", **common)
    assert "another" in str(approval_problem(record, branch="agent/ci/other", **common))
    assert "another" in str(
        approval_problem(record, **{**common, "run_id": "r2"}, branch="agent/ci/r")
    )
    assert "another" in str(
        approval_problem(record, **{**common, "project_id": "p2"}, branch="agent/ci/r")
    )
    for branch in ("main", "master"):
        with pytest.raises(CIError):
            check_ci_branch(branch, prefix="", base_branch="develop", default_branch="trunk")


def test_resume_problems() -> None:
    checkpoint = {
        "remote": "https://github.com/acme/app",
        "base_branch": "main",
        "checklist_id": "c",
        "checklist_version": 2,
        "pushed_commit": "c2",
    }
    ok: dict[str, Any] = {
        "remote": "https://github.com/acme/app",
        "base_branch": "main",
        "checklist_id": "c",
        "checklist_version": 2,
        "branch_head": "c2",
    }
    assert resume_problems(checkpoint, **ok) == []
    cases: dict[str, dict[str, Any]] = {
        "remote_changed": {"remote": "https://github.com/acme/fork"},
        "base_branch_changed": {"base_branch": "develop"},
        "checklist_changed": {"checklist_version": 3},
        "foreign_commits": {"branch_head": "someone-else"},
        "ci_branch_missing": {"branch_head": None},
    }
    for code, change in cases.items():
        assert [c for c, _ in resume_problems(checkpoint, **{**ok, **change})] == [code]


def test_the_agent_and_mcp_have_no_tool_to_push_approve_or_delete_a_branch() -> None:
    registry = get_registry()
    for audience in ("agent", "mcp"):
        for name in (
            "push", "git_push", "push_ci_branch", "ci_push", "approve_ci_loop",
            "ci_loop_approval", "revoke_ci_loop", "delete_ci_branch", "dispatch_workflow",
        ):  # fmt: skip
            assert registry.find(audience, name) is None
            with pytest.raises(UnknownToolError):
                registry.get(audience, name)
    for tool in registry.all():
        assert "push" not in tool.name and "branch" not in tool.name
        assert "dispatch" not in tool.name and "approv" not in tool.name


# ---- the loop through the API --------------------------------------------------------------------

BASELINE = report(
    OK,
    case("test_old", cls="tests.test_legacy", file="tests/test_legacy.py", fail="Error: legacy"),
    case("test_was_broken", cls="tests.test_fixme", file="tests/test_fixme.py", fail="E: b"),
)
FIRST = report(
    OK,
    case("test_old", cls="tests.test_legacy", file="tests/test_legacy.py", fail="Error: legacy"),
    case("test_was_broken", cls="tests.test_fixme", file="tests/test_fixme.py"),
    case("test_paid", file=TEST_FILE, fail="AssertionError: invoice 4411 not paid after 0.3s"),
    case("test_summary", cls="tests.test_report", file="tests/test_report.py",
         fail="KeyError: due", trace=f"File /home/runner/work/app/app/{INVOICE}:9 in due"),
    case("test_unrelated", cls="tests.test_other", file="tests/test_other.py",
         fail="ValueError: clock skew"),
    case("test_retry", cls="tests.test_net", file="tests/test_net.py", flaky=True),
)  # fmt: skip
SECOND = report(
    OK,
    case("test_old", cls="tests.test_legacy", file="tests/test_legacy.py", fail="Error: legacy"),
    case("test_was_broken", cls="tests.test_fixme", file="tests/test_fixme.py"),
    case("test_paid", file=TEST_FILE),
    case("test_summary", cls="tests.test_report", file="tests/test_report.py"),
    case("test_unrelated", cls="tests.test_other", file="tests/test_other.py"),
    case("test_retry", cls="tests.test_net", file="tests/test_net.py", flaky=True),
)
TEST_BODY = "def test_paid() -> None:\n    assert True\n"


def test_repair_iterations_until_green_then_a_proposed_patch(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    h.gh.reports = [("failure", BASELINE), ("failure", FIRST), ("failure", SECOND)]
    h.checker = ScriptedChecker([1, 0])  # the first edit alone has a finding: one batch of two
    provider = model(
        [
            write(INVOICE, invoice()),
            write(TEST_FILE, TEST_BODY),
            extend("\n\ndef due() -> None:\n    return None\n"),
        ]
    )
    run_id = start(client, world)
    branch = f"agent/ci/{run_id}"
    await_pending(client, world, run_id)
    assert h.gh.requests == []  # nothing before the approval
    info = client.get(run_url(world, run_id, "/ci-loop/approval-info"))
    assert info.status_code == 200, info.text
    assert info.json() == {
        "run_id": run_id,
        "coding_step": "S-01",
        "repository": "acme/app",
        "remote": "https://github.com/acme/app",
        "branch": branch,
        "base_branch": "main",
        "workflow": "archpilot-tests.yml",
        "command": "pytest",
        "max_iterations": 5,
        "protected_notice": ci_loop.PROTECTION_NOTICE,
        "approved": False,
    }
    approved = approve(client, world, run_id)
    assert approved["approval"] == "approved" and approved["approved_by"] == "Ivan"
    again = client.post(run_url(world, run_id, "/ci-loop/approval"), json={"approved_by": "Eve"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_decided"

    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("finished", "green"), run
    assert [s["action"] for s in run["steps"]] == ["write_file"] * 2 + ["edit_file", "finish"]

    # Two CI iterations after the baseline; commits chain on the CI branch only.
    gh = h.gh
    assert [r["head_sha"] for r in gh.runs.values()] == ["base0", "c1", "c2"]
    assert gh.refs == {"main": "base0", branch: "c2"}
    assert gh.commits["c1"]["parents"] == ["base0"] and gh.commits["c2"]["parents"] == ["c1"]
    assert f"ArchPilot-Run: {run_id}" in gh.commits["c2"]["message"]
    assert "ArchPilot-Iteration: 2" in gh.commits["c2"]["message"]
    assert [p["path"] for p in gh.trees[0]["tree"]] == [INVOICE, TEST_FILE]  # one batch
    assert all("/heads/main" not in r.url.path for r in gh.writes())
    assert [r.url.path.endswith("/git/refs") for r in gh.writes()].count(True) == 1
    inputs = [r["inputs"] for r in gh.runs.values()]
    assert inputs == [{"command": "pytest", "archpilot_run": run_id}] * 3

    # The model saw only actionable failures, flaky tests and counts.
    after_first = provider.requests[2].prompt.replace('\\"', '"')
    assert "tests.test_invoice::test_paid" in after_first
    assert "tests.test_report::test_summary" in after_first
    assert "linked_to_changed_file" in after_first
    assert "test_unrelated" not in after_first and "test_old" not in after_first
    assert "tests.test_net::test_retry" in after_first
    assert '"pre_existing": 1' in after_first and '"elsewhere": 2' in after_first
    assert len(provider.requests) == 3  # the green finish needs no model call

    patch = patch_of(client, world, run)
    notes = "\n".join(patch["notes"])
    assert "CI workflow: https://github.com/acme/app/actions/runs/1002" in notes
    assert "Final commit: c2" in notes and "CI iterations: 2" in notes
    assert "Tests: 1 failed, 6 passed" in notes
    assert "Fixed failures: " in notes and "tests.test_fixme::test_was_broken" in notes
    assert "tests.test_invoice::test_paid" in notes
    assert "Non-blocking: pre-existing failures: tests.test_legacy::test_old" in notes
    assert "Non-blocking: flaky tests: tests.test_net::test_retry" in notes
    assert patch["status"] == "proposed"  # never accepted, merged or opened as a PR
    assert not [r for r in gh.requests if "/pulls" in r.url.path and r.method == "POST"]
    assert steps_in(view(client, world))[0]["status"] == "proposed"

    loop = run["ci_loop"]
    assert loop["approval"] == "approved" and loop["approved_by"] == "Ivan"
    assert (loop["iteration"], loop["commit"], loop["green"]) == (2, "c2", True)
    assert loop["branch"] == branch and loop["terminal"] == "green"
    assert loop["workflow_url"].endswith("/actions/runs/1002")
    assert loop["baseline_url"].endswith("/actions/runs/1000")
    assert loop["counts"] == {"new": 0, "pre_existing": 1, "fixed": 4, "flaky": 1, "elsewhere": 0}
    assert loop["coding_step"] == "S-01" and loop["max_iterations"] == 5

    iterations = loop_rows(engine, run_id, ITERATION)
    assert [i["iteration"] for i in iterations] == [0, 1, 2]
    first = iterations[1]
    assert first["counts"] == {"new": 3, "pre_existing": 1, "fixed": 1, "flaky": 1, "elsewhere": 2}
    checkpoints = loop_rows(engine, run_id, CHECKPOINT)
    assert [c["pushed_commit"] for c in checkpoints] == ["c1", "c2"]
    last = checkpoints[-1]
    assert last["branch"] == branch and last["step_key"] == "S-01"
    assert last["parent_commit"] == "c1" and set(last["draft_hashes"]) == {INVOICE, TEST_FILE}
    assert last["iteration"] == 2 and last["latest_failures"]  # failures before this commit
    assert last["actions"] and last["tokens"]["input"] > 0 and "deployment" in last

    # Cleanup is offered, never automatic; a patch needs a confirmation; logs stay.
    gh.pulls = [{"number": 7}]
    used = client.post(run_url(world, run_id, "/ci-loop/delete-branch"), json={})
    assert used.json()["detail"]["code"] == "in_use"
    gh.pulls = []
    unconfirmed = client.post(run_url(world, run_id, "/ci-loop/delete-branch"), json={})
    assert unconfirmed.json()["detail"]["code"] == "confirm_required"
    assert branch in gh.refs
    deleted = client.post(run_url(world, run_id, "/ci-loop/delete-branch"), json={"confirm": True})
    assert deleted.status_code == 200 and deleted.json()["branch_deleted"] is True
    assert branch not in gh.refs and gh.refs["main"] == "base0"
    kept = loop_of(client, world, run_id)
    assert kept["iteration"] == 2 and kept["counts"]["pre_existing"] == 1
    assert len(loop_rows(engine, run_id, ITERATION)) == 3
    twice = client.post(run_url(world, run_id, "/ci-loop/delete-branch"), json={"confirm": True})
    assert twice.json()["detail"]["code"] == "already_deleted"


def test_a_static_failure_stops_before_ci(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    h.checker = ScriptedChecker([2])
    provider = model([write(INVOICE, invoice()), action("run_tests", command_id="pytest")])
    run_id = start(client, world, max_steps=2)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "step_limit"), run
    assert h.gh.requests == []  # nothing pushed, nothing asked
    assert run["ci_loop"]["approval"] == "none" and run["ci_loop"]["iteration"] == 0
    assert run["patch_id"] is None  # static findings: no reviewable patch
    assert run["steps"][1]["result_summary"] == "CI: static_failed"
    assert '"static_findings": 2' in provider.requests[-1].prompt or len(provider.requests) == 2


def test_edits_are_batched_into_one_commit(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    h.checker = ScriptedChecker([1, 1, 0])
    model(
        [
            write(INVOICE, invoice()),
            write("src/paid.py", "PAID = True\n"),
            write(TEST_FILE, TEST_BODY),
        ]
    )
    run_id = start(client, world)
    approve(client, world, run_id)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("finished", "green"), run
    assert len(h.gh.sent("POST", "/git/commits")) == 1 and len(h.gh.dispatches()) == 2
    assert [p["path"] for p in h.gh.trees[0]["tree"]] == [INVOICE, "src/paid.py", TEST_FILE]
    assert run["ci_loop"]["iteration"] == 1


def test_no_push_without_approval(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    model([write(INVOICE, invoice())])
    run_id = start(client, world)
    await_pending(client, world, run_id)
    denied = client.post(run_url(world, run_id, "/ci-loop/revoke"))
    assert denied.status_code == 200, denied.text
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "approval_revoked"), run
    assert h.gh.requests == [] and run["patch_id"] is None
    assert run["ci_loop"]["approval"] == "revoked"
    late = client.post(run_url(world, run_id, "/ci-loop/approval"), json={"approved_by": "Ivan"})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "not_running"


def test_an_unanswered_approval_times_out_without_any_request(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    h.config = LoopConfig(poll=0.01, ci_timeout=5, approval_timeout=0.2)
    model([write(INVOICE, invoice())])
    run = finished(client, world, h, start(client, world))
    assert run["stop_reason"] == "approval_revoked" and h.gh.requests == []


def test_a_default_branch_equal_to_the_ci_branch_is_never_pushed(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    model([write(INVOICE, invoice())])
    run_id = start(client, world)
    h.gh.default_branch = f"agent/ci/{run_id}"  # the repository's default is checked per push
    approve(client, world, run_id)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("failed", "ci_unavailable"), run
    assert h.gh.writes() == [] and h.gh.refs == {"main": "base0"}
    assert "default branch" in run["summary"]


def test_the_same_failure_three_times_is_not_converging(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)

    def failing(seconds: str, run: str) -> str:
        message = f"AssertionError: not paid after {seconds}s (run {run})"
        return report(OK, case("test_paid", file=TEST_FILE, fail=message))

    h.gh.reports = [
        ("success", report(OK)),
        ("failure", failing("0.1", "11")),
        ("failure", failing("2.5", "12")),
        ("failure", failing("0.9", "13")),
    ]
    model([write(INVOICE, invoice())] + [extend(f"\nN = {n}\n") for n in range(4)])
    run_id = start(client, world)
    approve(client, world, run_id)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "not_converging"), run
    assert len(h.gh.dispatches()) == 4  # baseline + 3 iterations, then it stops
    assert run["ci_loop"]["iteration"] == 3 and run["ci_loop"]["counts"]["new"] == 1
    # Deterministic validation passes: a reviewable patch labelled with the stop reason.
    notes = "\n".join(patch_of(client, world, run)["notes"])
    assert "Stopped: not_converging" in notes
    assert "Remaining failure: tests.test_invoice::test_paid" in notes


def test_the_iteration_limit_is_a_step_limit(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    h.config = LoopConfig(poll=0.01, ci_timeout=20, approval_timeout=10, max_iterations=1)
    h.gh.reports = [("success", report(OK)), ("failure", FIRST)]
    model([write(INVOICE, invoice()), extend("\nX = 1\n")])
    run_id = start(client, world)
    approve(client, world, run_id)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "step_limit"), run
    assert len(h.gh.sent("POST", "/git/commits")) == 1


def test_a_workflow_that_never_finishes_is_a_ci_timeout(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    h.config = LoopConfig(poll=0.01, ci_timeout=0.6, approval_timeout=10)
    h.gh.hang_from = 1
    model([write(INVOICE, invoice())])
    run_id = start(client, world)
    approve(client, world, run_id)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "ci_timeout"), run
    assert h.gh.runs[1001]["cancelled"] is True  # the workflow run is cancelled on GitHub
    assert "Stopped: ci_timeout" in "\n".join(patch_of(client, world, run)["notes"])


def test_the_wall_clock_limit_keeps_running_while_polling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, engine: Engine, world: World, h: Harness
) -> None:
    settings(monkeypatch, time_limit_seconds=1.5)
    approved_two_steps(client, engine, world)
    h.gh.hang_from = 1
    model([write(INVOICE, invoice())])
    began = time.monotonic()
    run_id = start(client, world)
    approve(client, world, run_id)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "time_limit"), run
    assert time.monotonic() - began < 10  # the 20 s CI timeout was not waited for
    assert h.gh.runs[1001]["cancelled"] is True


def test_stop_during_polling_cancels_the_workflow_and_commits_nothing_more(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    h.gh.hang_from = 1
    model([write(INVOICE, invoice()), extend("\nY = 2\n")])
    run_id = start(client, world)
    approve(client, world, run_id)
    wait_for(lambda: loop_of(client, world, run_id).get("phase") == "polling", 15)
    busy = client.post(run_url(world, run_id, "/ci-loop/delete-branch"), json={"confirm": True})
    assert busy.json()["detail"]["code"] == "not_ended"
    commits = len(h.gh.sent("POST", "/git/commits"))
    stopped = client.post(run_url(world, run_id, "/stop"))
    assert stopped.status_code == 200, stopped.text
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "cancelled"), run
    assert h.gh.runs[1001]["cancelled"] is True
    assert len(h.gh.sent("POST", "/git/commits")) == commits == 1  # none after the stop
    assert run["ci_loop"]["approval"] == "revoked" and run["patch_id"] is None
    revoked = loop_rows(engine, run_id, ci_loop.APPROVAL)[-1]
    assert revoked["revoked_at"] and revoked["revoked"] == "stopped"


def test_stop_while_waiting_for_approval_pushes_nothing(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    model([write(INVOICE, invoice())])
    run_id = start(client, world)
    await_pending(client, world, run_id)
    assert client.post(run_url(world, run_id, "/stop")).status_code == 200
    run = finished(client, world, h, run_id)
    assert run["stop_reason"] == "cancelled" and h.gh.requests == []


def stop_after_first_iteration(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> tuple[str, str]:
    """A run whose first CI iteration fails, then is stopped: a checkpoint to resume from."""
    approved_two_steps(client, engine, world)
    h.gh.reports = [("success", report(OK)), ("failure", FIRST), ("success", report(OK))]
    # The person stops the run while the model reacts to the first CI result.
    stop_after(engine, [write(INVOICE, invoice()), action("read_file", path=INVOICE)], 2)
    run_id = start(client, world)
    approve(client, world, run_id)
    h.join()
    h.threads.clear()
    run = get_run(client, world, run_id)
    assert (run["status"], run["stop_reason"]) == ("stopped", "cancelled"), run
    assert run["patch_id"] is None
    return run_id, f"agent/ci/{run_id}"


def test_a_stopped_run_resumes_from_its_checkpoint_on_the_same_branch(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    first_id, branch = stop_after_first_iteration(client, engine, world, h)
    assert h.gh.refs[branch] == "c1"
    provider = model([write(TEST_FILE, TEST_BODY)])
    response = client.post(url(world, "/steps/S-01/resume"), json={})
    assert response.status_code == 202, response.text
    second_id = response.json()["id"]
    assert second_id != first_id
    approve(client, world, second_id)  # a resumed run needs its own approval
    run = finished(client, world, h, second_id)
    assert (run["status"], run["stop_reason"]) == ("finished", "green"), run
    assert run["ci_loop"]["branch"] == branch and run["ci_loop"]["iteration"] == 2
    assert h.gh.refs[branch] == "c2" and h.gh.commits["c2"]["parents"] == ["c1"]
    assert "ArchPilot-Iteration: 2" in h.gh.commits["c2"]["message"]
    assert len(h.gh.dispatches()) == 3  # no second baseline
    assert len(h.gh.sent("POST", "/git/refs")) == 1
    assert "checkpoint" in provider.requests[0].prompt


def test_resume_is_refused_after_a_foreign_commit_or_a_repository_change(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    _, branch = stop_after_first_iteration(client, engine, world, h)
    h.gh.refs[branch] = "someone-else"
    foreign = client.post(url(world, "/steps/S-01/resume"), json={})
    assert foreign.status_code == 409, foreign.text
    assert foreign.json()["detail"]["code"] == "foreign_commits"
    assert foreign.json()["detail"]["problems"] == ["foreign_commits"]

    h.gh.refs[branch] = "c1"
    h.targets[world[0]] = target(remote=parse_remote("https://github.com/acme/fork.git"))
    moved = client.post(url(world, "/steps/S-01/resume"), json={})
    assert moved.status_code == 409 and moved.json()["detail"]["code"] == "remote_changed"
    assert PAT not in moved.text and PAT not in foreign.text


def test_the_token_never_reaches_logs_urls_errors_responses_or_the_model(
    client: TestClient,
    engine: Engine,
    world: World,
    h: Harness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    approved_two_steps(client, engine, world)
    leaked = report(
        OK,
        case(
            "test_paid",
            file=TEST_FILE,
            fail=f"Error: token {PAT} rejected",
            trace=f"File {INVOICE}:3 in pay",
        ),
    )
    h.gh.reports = [("success", report(OK)), ("failure", leaked)]
    h.gh.fail[("POST", "/actions/workflows/archpilot-tests.yml/dispatches")] = (2, 500)
    provider = model([write(INVOICE, invoice()), extend("\nZ = 3\n")])
    run_id = start(client, world)
    approve(client, world, run_id)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("failed", "ci_unavailable"), run
    assert "HTTP 500" in run["summary"]
    everything = json.dumps(run) + caplog.text + client.get(api(world[0], "/agent-runs")).text
    everything += json.dumps(loop_rows(engine, run_id, ITERATION))
    everything += json.dumps(loop_rows(engine, run_id, CHECKPOINT))
    everything += client.get(run_url(world, run_id, "/ci-loop/approval-info")).text
    assert PAT not in everything
    assert all(
        PAT not in r.prompt and PAT not in (r.system_prompt or "") for r in provider.requests
    )
    assert "token *** rejected" in provider.requests[1].prompt  # the CI message, scrubbed
    for request in h.gh.requests:
        assert PAT not in str(request.url)
        if request.url.host == "storage.example.net":
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == f"Bearer {PAT}"


def test_projects_and_runs_are_isolated(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    approved_two_steps(client, engine, world)
    model([write(INVOICE, invoice())])
    run_id = start(client, world)
    await_pending(client, world, run_id)
    other, _, _ = approved_project(client)
    for response in (
        client.get(api(other, f"/agent-runs/{run_id}")),
        client.get(api(other, f"/agent-runs/{run_id}/ci-loop/approval-info")),
        client.post(
            api(other, f"/agent-runs/{run_id}/ci-loop/approval"), json={"approved_by": "x"}
        ),
        client.post(api(other, f"/agent-runs/{run_id}/ci-loop/revoke")),
        client.post(api(other, f"/agent-runs/{run_id}/ci-loop/delete-branch"), json={}),
    ):
        assert response.status_code == 404, response.text
    assert ci_loop.loop_state(uuid.UUID(other), uuid.UUID(run_id)) is None
    assert loop_of(client, world, run_id)["approval"] == "pending"  # untouched
    assert client.post(run_url(world, run_id, "/stop")).status_code == 200
    h.join()


def test_a_project_without_a_connected_repository_has_no_ci_loop(
    client: TestClient, engine: Engine, world: World, h: Harness
) -> None:
    h.targets.clear()
    approved_two_steps(client, engine, world)
    model([write(INVOICE, invoice()), action("finish", summary="done")])
    run_id = start(client, world)
    run = finished(client, world, h, run_id)
    assert (run["status"], run["stop_reason"]) == ("finished", "finished"), run
    assert run["ci_loop"] is None and h.gh.requests == []
    info = client.get(run_url(world, run_id, "/ci-loop/approval-info"))
    assert info.status_code == 409 and info.json()["detail"]["code"] == "ci_unavailable"
