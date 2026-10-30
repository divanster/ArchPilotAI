"""GitHub Actions execution provider (Phase 13B). The GitHub API is always a mock transport: no
request ever leaves the process, and nothing runs locally."""

import io
import json
import logging
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api import deps
from app.core.config import get_settings
from app.domain.execution import MAX_REPORT_BYTES, ProviderUnavailableError, UnknownCommandError
from app.domain.git_remote import parse_remote
from app.models.git_remote import GitRemoteSettings
from app.services import ci_execution
from app.services.ci_execution import (
    ARTIFACT_NAME,
    DENIED_MESSAGE,
    CIApprovalError,
    CIError,
    CIExecutionProvider,
    CITarget,
    check_ci_branch,
    ci_branch,
    ci_state,
    decide,
    git_blob_sha,
    junit_from_zip,
    release_state,
    settings_targets,
    workflow_template,
)
from app.services.execution import TestRunner, release_run
from app.tools import UnknownToolError, get_registry
from tests.test_agent import action, configure, finish, scripted, start, write
from tests.test_agent import clean_agent_state as _clean_agent_state
from tests.test_agent import world as _agent_world
from tests.test_patches import World, api

world = _agent_world
clean_agent_state = _clean_agent_state  # autouse: runs the agent inline

PAT = "ghp_SuperSecretToken1234567890"
API = "https://api.github.com"
REPO = f"{API}/repos/acme/app"
STORAGE = "https://storage.example.net/artifact.zip?sig=signed"
WORKFLOW_RUN = 4242

ACCEPTED = {"src/a.py": "a = 1\n", "src/gone.py": "x = 0\n", "src/same.py": "v1\n"}
DRAFT = {"src/a.py": "a = 2\n", "src/new.py": "n = 1\n", "src/same.py": "v2\n"}
BASE_TREE = [
    {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": git_blob_sha("a = 1\n")},
    {"path": "src/gone.py", "mode": "100644", "type": "blob", "sha": git_blob_sha("x = 0\n")},
    {"path": "src/same.py", "mode": "100755", "type": "blob", "sha": git_blob_sha("v2\n")},
    {"path": "src", "mode": "040000", "type": "tree", "sha": "t-src"},
]


def junit_xml(*failing: str) -> str:
    cases = '<testcase classname="tests.test_ok" name="test_fine"/>'
    for name in failing:
        cases += (
            f'<testcase classname="tests.test_a" name="{name}" file="src/a.py">'
            '<failure message="assert 2 == 1">tb</failure></testcase>'
        )
    return f"<testsuites><testsuite>{cases}</testsuite></testsuites>"


def zipped(members: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeGitHub:
    """The GitHub REST endpoints the provider uses, scripted, recording every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.default_branch = "main"
        self.statuses = ["queued", "in_progress", "completed"]
        self.conclusion = "failure"
        self.artifact: bytes | None = zipped({"junit.xml": junit_xml("test_two")})
        self.archive_url = f"{REPO}/actions/artifacts/9/zip"
        self.redirect = STORAGE
        self.branch_message: str | None = None  # an existing CI branch's head commit message
        self.lookups_before_run = 1
        self.rate_limited: dict[str, list[httpx.Response]] = {}
        self.fail: dict[tuple[str, str], int] = {}
        self.refs: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), follow_redirects=False)

    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    def body(self, request: httpx.Request) -> Any:
        return json.loads(request.content or b"null")

    def sent(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://storage.example.net/"):
            return httpx.Response(200, content=self.artifact or b"")
        path = request.url.path.removeprefix("/repos/acme/app")
        key = (request.method, path)
        if key in self.fail:
            return httpx.Response(self.fail[key], json={"message": f"echo {PAT}"})
        queued = self.rate_limited.get(path)
        if queued:
            return queued.pop(0)
        method = request.method
        if method == "GET" and path == "":
            return httpx.Response(200, json={"default_branch": self.default_branch})
        if method == "GET" and path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base0"}})
        if method == "GET" and path == "/git/commits/base0":
            return httpx.Response(200, json={"tree": {"sha": "tree0"}, "message": "base"})
        if method == "GET" and path == "/git/trees/tree0":
            return httpx.Response(200, json={"tree": BASE_TREE, "truncated": False})
        if method == "POST" and path == "/git/trees":
            return httpx.Response(201, json={"sha": "tree1"})
        if method == "POST" and path == "/git/commits":
            return httpx.Response(201, json={"sha": "commit1"})
        if method == "GET" and path.startswith("/git/ref/heads/"):
            if self.branch_message is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": "old1"}})
        if method == "GET" and path == "/git/commits/old1":
            return httpx.Response(200, json={"message": self.branch_message, "tree": {}})
        if method == "POST" and path == "/git/refs":
            self.refs.add(self.body(request)["ref"])
            return httpx.Response(201, json={})
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            return httpx.Response(200, json={})
        if method == "DELETE" and path.startswith("/git/refs/heads/"):
            return httpx.Response(204)
        if method == "POST" and path.endswith("/dispatches"):
            return httpx.Response(204)
        if method == "GET" and path.endswith("/archpilot-tests.yml/runs"):
            if self.lookups_before_run > 0:
                self.lookups_before_run -= 1
                return httpx.Response(200, json={"workflow_runs": []})
            run = {"id": WORKFLOW_RUN, "head_sha": request.url.params.get("head_sha")}
            return httpx.Response(200, json={"workflow_runs": [run]})
        if method == "GET" and path == f"/actions/runs/{WORKFLOW_RUN}":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={"status": status, "conclusion": self.conclusion})
        if method == "POST" and path == f"/actions/runs/{WORKFLOW_RUN}/cancel":
            return httpx.Response(202, json={})
        if method == "GET" and path == f"/actions/runs/{WORKFLOW_RUN}/artifacts":
            items = []
            if self.artifact is not None:
                items.append(
                    {
                        "name": ARTIFACT_NAME,
                        "expired": False,
                        "size_in_bytes": len(self.artifact),
                        "archive_download_url": self.archive_url,
                    }
                )
            return httpx.Response(200, json={"artifacts": items})
        if method == "GET" and path == "/actions/artifacts/9/zip":
            return httpx.Response(302, headers={"location": self.redirect})
        return httpx.Response(599, json={"message": f"unexpected {method} {path}"})


def target(**changes: Any) -> CITarget:
    values: dict[str, Any] = {
        "provider": "github",
        "remote": parse_remote("https://github.com/acme/app.git"),
        "base_branch": "main",
        "branch_prefix": "agent/",
        "token": PAT,
    }
    values.update(changes)
    return CITarget(**values)


class Setup:
    def __init__(self, gh: FakeGitHub, **options: Any) -> None:
        self.gh = gh
        self.project_id = uuid.uuid4()
        self.run_id = uuid.uuid4()
        self.target = options.pop("target", None) or target()
        accepted = options.pop("accepted", ACCEPTED)
        unbound = CIExecutionProvider(
            lambda _pid: self.target,
            lambda _pid: accepted,
            client=gh.client(),
            poll_interval=options.pop("poll", 0.01),
            approval_timeout=options.pop("approval_timeout", 5.0),
            **options,
        )
        self.provider = unbound.bind(self.project_id, self.run_id)

    def run(
        self, files: dict[str, str] | None = None, timeout: float = 5.0, job: str = "j1"
    ) -> Any:
        return self.provider.run("pytest", files or DRAFT, timeout, 4000, job_id=job)

    def state(self) -> dict[str, Any] | None:
        return ci_state(self.project_id, self.run_id)


def answer_when_pending(
    setup: Setup, approve: bool = True, keep: bool = False, who: str = "Ivan"
) -> threading.Thread:
    def work() -> None:
        for _ in range(500):
            found = setup.state()
            if found and found["approval"] == "pending":
                decide(
                    setup.project_id,
                    setup.run_id,
                    approve=approve,
                    approved_by=who,
                    keep_branches=keep,
                )
                return
            time.sleep(0.01)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def wait_for(check: Callable[[], bool], seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.fixture(autouse=True)
def clean_ci_state() -> Iterator[None]:
    yield
    with ci_execution._cond:
        ci_execution._states.clear()


# ---- the flow: push, dispatch, poll, artifact, parse ------------------------------------------


def test_ci_job_pushes_dispatches_polls_and_reads_the_junit_artifact() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    answer_when_pending(setup, who="Ivan")
    outcome = TestRunner(setup.provider, timeout=5, output_cap=4000).run(
        setup.run_id, "pytest", DRAFT, ACCEPTED, ["src/a.py", "src/new.py"]
    )
    payload = outcome.payload
    assert payload["status"] == "failed" and payload["structured"] is True
    assert payload["baseline"] is False  # the base branch is the baseline: no second CI run
    assert payload["total"] == 2 and payload["new_in_changed"] == 1
    assert payload["new"][0]["name"] == "tests.test_a::test_two"
    assert (
        payload["ci"]["workflow_url"] == f"https://github.com/acme/app/actions/runs/{WORKFLOW_RUN}"
    )
    assert "approved_by" not in payload["ci"]  # the model never gets a person's name
    assert [j.variant for j in outcome.jobs] == ["changed"]  # no baseline job, no lone re-runs
    ci = outcome.jobs[0].summary["ci"]
    branch = f"agent/ci/{setup.run_id}"
    assert ci["branch"] == branch and ci["approved_by"] == "Ivan"
    assert ci["workflow_run_id"] == WORKFLOW_RUN and ci["conclusion"] == "failure"
    assert ci["status"] == "completed" and ci["report"] is True
    assert ci["branch_deleted"] is True and ci["kept"] is False

    tree = gh.body(gh.sent("POST", "/git/trees")[0])
    assert tree["base_tree"] == "tree0"
    entries = {e["path"]: e for e in tree["tree"]}
    assert set(entries) == {"src/a.py", "src/new.py", "src/gone.py"}  # src/same.py: unchanged
    assert entries["src/a.py"]["content"] == "a = 2\n"
    assert entries["src/gone.py"]["sha"] is None  # a deletion
    commit = gh.body(gh.sent("POST", "/git/commits")[0])
    assert commit["parents"] == ["base0"] and commit["tree"] == "tree1"
    assert f"ArchPilot-Run: {setup.run_id}" in commit["message"]
    assert f"ArchPilot-Project: {setup.project_id}" in commit["message"]
    assert gh.refs == {f"refs/heads/{branch}"}
    dispatch = gh.body(gh.sent("POST", "/actions/workflows/archpilot-tests.yml/dispatches")[0])
    assert dispatch == {
        "ref": branch,
        "inputs": {"command": "pytest", "archpilot_run": str(setup.run_id)},
    }
    assert gh.sent("DELETE", f"/git/refs/heads/{branch}")
    # Never a write to the base branch.
    assert not [r for r in gh.writes() if "heads/main" in r.url.path]


def test_success_without_failures_and_missing_artifact() -> None:
    gh = FakeGitHub()
    gh.conclusion = "success"
    gh.artifact = None
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run()
    assert result.exit_code == 0 and result.report is None
    assert "No archpilot-junit artifact" in result.stdout_tail
    assert result.meta is not None and result.meta["ci"]["report"] is False


def test_a_kept_branch_is_not_deleted_and_is_reused_by_the_next_job() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    answer_when_pending(setup, keep=True)
    first = setup.run(job="j1")
    assert first.meta is not None
    assert first.meta["ci"]["kept"] is True and first.meta["ci"]["branch_deleted"] is False
    assert gh.sent("DELETE", f"/git/refs/heads/agent/ci/{setup.run_id}") == []
    # The branch exists now; it carries this run's marker, so it is fast-forwarded.
    gh.branch_message = f"ArchPilotAI CI run\n\nArchPilot-Run: {setup.run_id}\n"
    gh.statuses = ["completed"]
    second = setup.run(job="j2")
    assert second.exit_code == 1
    patch = gh.sent("PATCH", f"/git/refs/heads/agent/ci/{setup.run_id}")
    assert len(patch) == 1 and gh.body(patch[0]) == {"sha": "commit1", "force": False}
    assert gh.body(gh.sent("POST", "/git/commits")[-1])["parents"] == ["old1"]


def test_an_existing_branch_of_someone_else_is_refused() -> None:
    gh = FakeGitHub()
    gh.branch_message = "a person's own work"
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run()
    assert result.exit_code is None and "not created by this run" in result.stderr_tail
    assert gh.sent("POST", "/git/refs") == [] and gh.sent("PATCH", "") == []
    assert gh.sent("POST", "/dispatches") == []


# ---- refusals ---------------------------------------------------------------------------------


def test_check_ci_branch_refuses_base_default_and_protected_names() -> None:
    run_id = uuid.uuid4()
    good = ci_branch("agent/", run_id)
    assert good == f"agent/ci/{run_id}"
    assert check_ci_branch(good, prefix="agent/", base_branch="main", default_branch="main")
    for branch, base, default in [
        ("main", "main", "main"),
        ("master", "develop", "develop"),
        ("develop", "develop", "main"),
        ("trunk", "main", "trunk"),
        ("agent/feature", "main", "main"),  # not under <prefix>ci/
        ("agent/ci/..x", "main", "main"),  # not a valid branch name
    ]:
        with pytest.raises(CIError):
            check_ci_branch(branch, prefix="agent/", base_branch=base, default_branch=default)


def test_the_base_branch_is_never_the_ci_branch() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    setup.target = target(base_branch=f"agent/ci/{setup.run_id}")
    result = setup.run()
    assert result.exit_code is None and "base or default branch" in result.stderr_tail
    assert gh.requests == []  # refused before asking, before any call
    assert (setup.state() or {})["approval"] == "none"


def test_the_repository_default_branch_is_never_the_ci_branch() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    gh.default_branch = f"agent/ci/{setup.run_id}"
    answer_when_pending(setup)
    result = setup.run()
    assert "base or default branch" in result.stderr_tail
    assert gh.writes() == []


def test_changes_under_github_dir_are_never_pushed() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    files = {**DRAFT, ".github/workflows/evil.yml": "on: push\n"}
    result = setup.run(files)
    assert ".github/" in result.stderr_tail and gh.requests == []


def test_select_and_unknown_commands_are_refused() -> None:
    setup = Setup(FakeGitHub())
    with pytest.raises(UnknownCommandError):
        setup.provider.run("rm -rf /", DRAFT, 1, 100, job_id="x")
    with pytest.raises(UnknownCommandError):
        setup.provider.run("pytest", DRAFT, 1, 100, job_id="x", select="tests::t")
    unbound = CIExecutionProvider(lambda _p: target(), lambda _p: {})
    assert unbound.status() == (False, "The CI provider is not bound to a run")
    with pytest.raises(ProviderUnavailableError):
        unbound.run("pytest", DRAFT, 1, 100)


# ---- approval -----------------------------------------------------------------------------------


def test_nothing_is_pushed_until_the_user_approves() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(result=setup.run()))
    thread.start()
    wait_for(lambda: (setup.state() or {}).get("approval") == "pending")
    time.sleep(0.05)
    assert gh.requests == []  # waiting: not a single API call
    assert (setup.state() or {})["phase"] == "awaiting_approval"
    decide(setup.project_id, setup.run_id, approve=True, approved_by="Ivan")
    thread.join(5)
    assert box["result"].exit_code == 1 and gh.sent("POST", "/git/refs")


def test_a_refusal_pushes_nothing_and_is_not_asked_again() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    answer_when_pending(setup, approve=False)
    first = setup.run(job="j1")
    assert first.cancelled and DENIED_MESSAGE in first.stderr_tail
    assert first.meta is not None and first.meta["ci"]["status"] == "denied"
    second = setup.run(job="j2")  # once per run: no new question
    assert second.cancelled and DENIED_MESSAGE in second.stderr_tail
    assert gh.requests == []


def test_an_approval_covers_the_later_jobs_of_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    answer_when_pending(setup)
    setup.run(job="j1")
    gh.statuses = ["completed"]
    asked: list[str] = []
    original = CIExecutionProvider._await_approval

    def spy(self: Any, state: Any, cancelled: Any, deadline: float) -> str:
        asked.append(state.approval)
        return original(self, state, cancelled, deadline)

    monkeypatch.setattr(CIExecutionProvider, "_await_approval", spy)
    second = setup.run(job="j2")  # nobody answers: it must not ask
    assert asked == ["approved"] and second.exit_code == 1


def test_no_answer_times_out_without_any_call() -> None:
    gh = FakeGitHub()
    setup = Setup(gh, approval_timeout=0.1)
    result = setup.run()
    assert result.timed_out and "No answer" in result.stderr_tail
    assert gh.requests == [] and (setup.state() or {})["approval"] == "none"


def test_decide_needs_a_pending_question_of_the_same_project() -> None:
    setup = Setup(FakeGitHub())
    with pytest.raises(CIApprovalError) as missing:
        decide(setup.project_id, setup.run_id, approve=True, approved_by="x")
    assert missing.value.code == "not_found"
    thread = threading.Thread(target=setup.run)
    thread.start()
    wait_for(lambda: (setup.state() or {}).get("approval") == "pending")
    other = uuid.uuid4()
    assert ci_state(other, setup.run_id) is None  # project isolation
    with pytest.raises(CIApprovalError) as foreign:
        decide(other, setup.run_id, approve=True, approved_by="x")
    assert foreign.value.code == "not_found"
    decide(setup.project_id, setup.run_id, approve=False, approved_by="x")
    with pytest.raises(CIApprovalError) as twice:
        decide(setup.project_id, setup.run_id, approve=True, approved_by="x")
    assert twice.value.code == "not_pending"
    thread.join(5)
    release_state(setup.run_id)
    assert setup.state() is None


# ---- timeout, cancel, rate limit ---------------------------------------------------------------


def test_timeout_cancels_the_workflow_run_and_deletes_the_branch() -> None:
    gh = FakeGitHub()
    gh.statuses = ["in_progress"]
    setup = Setup(gh)
    answer_when_pending(setup)
    started = time.monotonic()
    result = setup.run(timeout=0.3)
    assert result.timed_out and result.exit_code is None
    assert time.monotonic() - started < 3
    assert gh.sent("POST", f"/actions/runs/{WORKFLOW_RUN}/cancel")
    assert gh.sent("DELETE", f"/git/refs/heads/agent/ci/{setup.run_id}")
    assert result.meta is not None and result.meta["ci"]["status"] == "timed_out"


def test_cancel_wakes_the_job_at_once() -> None:
    gh = FakeGitHub()
    gh.statuses = ["in_progress"]
    gh.lookups_before_run = 0
    setup = Setup(gh, poll=30.0)  # a long poll interval: only the cancel can wake it
    answer_when_pending(setup)
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(result=setup.run(timeout=60, job="jx")))
    thread.start()
    wait_for(lambda: (setup.state() or {}).get("phase") in ("queued", "in_progress"))
    wait_for(lambda: bool(gh.sent("GET", f"/actions/runs/{WORKFLOW_RUN}")))
    started = time.monotonic()
    setup.provider.cancel("jx")
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - started < 3
    assert box["result"].cancelled
    assert gh.sent("POST", f"/actions/runs/{WORKFLOW_RUN}/cancel")
    assert gh.sent("DELETE", f"/git/refs/heads/agent/ci/{setup.run_id}")


def test_cancel_while_waiting_for_approval_pushes_nothing() -> None:
    gh = FakeGitHub()
    setup = Setup(gh)
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.update(result=setup.run(job="jw")))
    thread.start()
    wait_for(lambda: (setup.state() or {}).get("approval") == "pending")
    setup.provider.cancel("jw")
    thread.join(5)
    assert box["result"].cancelled and gh.requests == []


def test_a_workflow_cancelled_on_github_is_a_cancelled_job() -> None:
    gh = FakeGitHub()
    gh.statuses = ["completed"]
    gh.conclusion = "cancelled"
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run()
    assert result.cancelled and gh.sent("GET", f"/actions/runs/{WORKFLOW_RUN}/artifacts") == []


def test_rate_limits_are_waited_out_then_retried() -> None:
    gh = FakeGitHub()
    gh.rate_limited[""] = [
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "0"}),
        httpx.Response(429, headers={"retry-after": "0"}),
    ]
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run()
    assert result.exit_code == 1
    assert len([r for r in gh.requests if r.url.path == "/repos/acme/app"]) == 3


def test_a_rate_limit_beyond_the_deadline_fails_cleanly() -> None:
    gh = FakeGitHub()
    gh.rate_limited[""] = [
        httpx.Response(
            403,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(time.time()) + 3600),
            },
        )
    ]
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run(timeout=5)
    assert result.exit_code is None and "rate limit" in result.stderr_tail
    assert gh.writes() == []


# ---- artifact handling --------------------------------------------------------------------------


def test_junit_from_zip_prefers_junit_xml_and_refuses_bombs() -> None:
    xml = junit_xml()
    assert junit_from_zip(zipped({"a/other.xml": "<x/>", "out/junit.xml": xml})) == xml
    assert junit_from_zip(zipped({"report.xml": xml, "notes.txt": "hi"})) == xml
    big = "<testsuites>" + " " * (MAX_REPORT_BYTES + 10) + "</testsuites>"
    assert junit_from_zip(zipped({"junit.xml": big})) is None  # declared size too large
    assert junit_from_zip(b"not a zip") is None
    assert junit_from_zip(zipped({"readme.md": "x"})) is None


def test_a_refused_report_falls_back_to_the_exit_code() -> None:
    gh = FakeGitHub()
    gh.artifact = zipped({"junit.xml": '<!DOCTYPE x [<!ENTITY a "b">]><testsuites/>'})
    setup = Setup(gh)
    answer_when_pending(setup)
    outcome = TestRunner(setup.provider, timeout=5, output_cap=4000).run(
        setup.run_id, "pytest", DRAFT, ACCEPTED, []
    )
    assert outcome.payload["structured"] is False and outcome.payload["status"] == "failed"


def test_artifact_downloads_stay_on_the_api_host_and_https() -> None:
    gh = FakeGitHub()
    gh.archive_url = "https://evil.example.com/steal.zip"
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run()
    assert "not on the GitHub API host" in result.stderr_tail
    assert not [r for r in gh.requests if r.url.host == "evil.example.com"]

    gh2 = FakeGitHub()
    gh2.redirect = "http://storage.example.net/plain.zip"
    setup2 = Setup(gh2)
    answer_when_pending(setup2)
    assert "not HTTPS" in setup2.run().stderr_tail


# ---- the token ----------------------------------------------------------------------------------


def test_the_token_is_only_in_the_api_authorization_header(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gh = FakeGitHub()
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run()
    assert gh.requests
    for request in gh.requests:
        assert PAT not in str(request.url)
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == "Bearer " + PAT
        else:  # the artifact storage redirect never gets the token
            assert "authorization" not in request.headers
        assert PAT.encode() not in (request.content or b"")
    assert any(r.url.host == "storage.example.net" for r in gh.requests)
    stored = json.dumps(result.meta) + result.log + result.stdout_tail + result.stderr_tail
    assert PAT not in stored and PAT not in caplog.text
    assert PAT not in repr(setup.target)


def test_error_messages_never_carry_the_token_or_a_response_body() -> None:
    gh = FakeGitHub()
    gh.fail[("POST", "/actions/workflows/archpilot-tests.yml/dispatches")] = 404
    setup = Setup(gh)
    answer_when_pending(setup)
    result = setup.run()
    assert result.exit_code is None
    assert "workflow dispatch: HTTP 404" in result.stderr_tail
    assert PAT not in result.log and "echo" not in result.log
    assert result.meta is not None and PAT not in json.dumps(result.meta)
    assert gh.sent("DELETE", f"/git/refs/heads/agent/ci/{setup.run_id}")  # still cleaned up


# ---- settings: token and repository -------------------------------------------------------------


class Store:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets

    def available(self) -> bool:
        return True

    def set(self, reference: str, secret: str) -> None:
        self.secrets[reference] = secret

    def get(self, reference: str) -> str | None:
        return self.secrets.get(reference)

    def delete(self, reference: str) -> None:
        self.secrets.pop(reference, None)


def test_settings_target_needs_a_github_repository_and_a_saved_token(
    engine: Engine, world: World
) -> None:
    pid = uuid.UUID(world[0])
    with Session(engine) as session:
        load = settings_targets(session, Store({"ref": PAT}))
        with pytest.raises(ProviderUnavailableError, match="No Git repository"):
            load(pid)
        row = GitRemoteSettings(
            project_id=pid,
            provider="gitlab",
            remote_url="https://gitlab.example.com/acme/app.git",
            clone_folder="acme-app",
            base_branch="main",
            branch_prefix="agent/",
            author_name="A",
            author_email="a@example.com",
            auth_method="pat",
        )
        session.add(row)
        session.commit()
        with pytest.raises(ProviderUnavailableError, match="GitHub"):
            load(pid)
        row.provider, row.remote_url = "github", "https://github.com/acme/app.git"
        session.commit()
        with pytest.raises(ProviderUnavailableError, match="personal access token"):
            load(pid)
        row.pat_ref, row.pat_provider, row.pat_last4 = "ref", "github", PAT[-4:]
        session.commit()
        found = load(pid)
        assert (found.owner, found.repo, found.token) == ("acme", "app", PAT)
        assert found.api_base == API
        with pytest.raises(ProviderUnavailableError, match="not found"):
            settings_targets(session, Store({}))(pid)
        provider = CIExecutionProvider(load, lambda _p: {}).bind(pid, uuid.uuid4())
        assert provider.status() == (True, "")
        assert (
            CIExecutionProvider(load, lambda _p: {}).bind(uuid.uuid4(), uuid.uuid4()).status()[0]
            is False
        )  # another project has no settings


def test_the_configured_provider_is_built_from_settings(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(engine) as session:
        assert deps.make_execution_provider(session).name == "unavailable"
        patched = get_settings().model_copy(update={"agent_execution_provider": "github_actions"})
        monkeypatch.setattr("app.api.deps.get_settings", lambda: patched)
        assert deps.make_execution_provider(session).name == "github_actions"
        service = deps.make_agent_service(session, deps._NoGeneration())
        assert service._tests._timeout == patched.agent_ci_timeout_seconds


def test_enterprise_api_and_html_urls() -> None:
    found = target(provider="github_enterprise", remote=parse_remote("https://git.corp/acme/app"))
    assert found.api_base == "https://git.corp/api/v3"
    assert found.html_repo == "https://git.corp/acme/app"


# ---- tools and template -------------------------------------------------------------------------


def test_the_agent_has_no_tool_to_push_or_approve_a_ci_branch() -> None:
    """The CI push is approval-tier: only a person answers, through the approval endpoint. No
    tool of any tier names it, so the agent cannot even ask to call it."""
    registry = get_registry()
    for audience in ("agent", "mcp"):
        for name in ("push_ci_branch", "approve_ci_push", "ci_approval"):
            assert registry.find(audience, name) is None
            with pytest.raises(UnknownToolError):
                registry.get(audience, name)
    for tool in registry.all():
        assert "ci/" not in tool.description and "approval" not in tool.name


def test_workflow_template_is_served_and_safe(client: TestClient) -> None:
    response = client.get("/api/agent/ci/workflow-template")
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == ".github/workflows/archpilot-tests.yml"
    assert body["artifact"] == ARTIFACT_NAME and body["content"] == workflow_template()
    text = body["content"]
    assert "workflow_dispatch" in text and f"name: {ARTIFACT_NAME}" in text
    assert "contents: read" in text and "secrets." not in text
    assert "persist-credentials: false" in text
    for line in text.splitlines():  # inputs reach the shell only through env variables
        if "${{" in line:
            assert line.strip().split(":", 1)[0] in {"run-name", "group", "if", "COMMAND"}, line


# ---- through the agent and the API --------------------------------------------------------------


def _real_threads(monkeypatch: pytest.MonkeyPatch) -> list[threading.Thread]:
    threads: list[threading.Thread] = []

    def spawn(job: Callable[[], None]) -> None:
        thread = threading.Thread(target=job, daemon=True)
        threads.append(thread)
        thread.start()

    monkeypatch.setattr("app.api.agent._spawn", spawn)
    return threads


def test_agent_run_asks_once_shows_ci_state_and_stores_the_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    gh = FakeGitHub()
    provider = CIExecutionProvider(
        lambda _p: target(), lambda _p: {}, client=gh.client(), poll_interval=0.01
    )
    configure(monkeypatch, execution=provider)
    threads = _real_threads(monkeypatch)
    model = scripted(
        [write("src/a.py", "a = 2\n"), action("run_tests", command_id="pytest"), finish()]
    )
    response = start(client, world)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    pid = world[0]
    url = api(pid, f"/agent-runs/{run_id}")

    def pending() -> bool:
        ci = client.get(url).json().get("ci")
        return bool(ci) and ci["approval"] == "pending"

    wait_for(pending, 10)
    assert gh.requests == []
    live = client.get(url).json()["ci"]
    assert live["phase"] == "awaiting_approval" and live["command_id"] == "pytest"

    other = "00000000-0000-4000-8000-000000000000"
    body = {"approve": True, "approved_by": "Ivan", "keep_branches": False}
    assert (
        client.post(api(other, f"/agent-runs/{run_id}/ci/approval"), json=body).status_code == 404
    )
    assert client.get(api(other, f"/agent-runs/{run_id}")).status_code == 404
    answered = client.post(f"{url}/ci/approval", json=body)
    assert answered.status_code == 200 and answered.json()["approval"] == "approved"
    assert client.post(f"{url}/ci/approval", json=body).status_code == 409  # once per run

    for thread in threads:
        thread.join(10)
    run = client.get(url).json()
    assert run["status"] == "finished" and run["ci"] is None
    step = run["steps"][1]
    assert step["tests"]["ci"]["workflow_url"].endswith(f"/actions/runs/{WORKFLOW_RUN}")
    assert step["tests"]["new_in_changed"] == 1
    [job] = run["jobs"]
    assert job["variant"] == "changed" and job["ci"]["approved_by"] == "Ivan"
    assert job["ci"]["branch"] == f"agent/ci/{run_id}" and job["ci"]["branch_deleted"] is True
    assert PAT not in json.dumps(run)
    assert all(PAT not in r.prompt for r in model.requests)
    assert ci_state(uuid.UUID(pid), uuid.UUID(run_id)) is None  # released with the run
    release_run(uuid.UUID(run_id))


def test_stopping_the_run_cancels_a_ci_job_waiting_for_approval(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    gh = FakeGitHub()
    provider = CIExecutionProvider(lambda _p: target(), lambda _p: {}, client=gh.client())
    configure(monkeypatch, execution=provider)
    threads = _real_threads(monkeypatch)
    scripted([action("run_tests", command_id="pytest"), finish()])
    run_id = start(client, world).json()["id"]
    url = api(world[0], f"/agent-runs/{run_id}")
    wait_for(lambda: (client.get(url).json().get("ci") or {}).get("approval") == "pending", 10)
    assert client.post(f"{url}/stop").status_code == 200
    for thread in threads:
        thread.join(10)
    run = client.get(url).json()
    assert run["status"] == "stopped" or run["stop_reason"] == "user_stop"
    assert [j["status"] for j in run["jobs"]] == ["cancelled"]
    assert gh.requests == []
    assert (
        client.post(f"{url}/ci/approval", json={"approve": True, "approved_by": "x"}).status_code
        == 409
    )
