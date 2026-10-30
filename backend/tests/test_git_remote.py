import ast
import logging
import re
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.api.git_remote import get_git_service
from app.domain.git_remote import (
    GitSettingsError,
    branch_name,
    compare_url,
    parse_remote,
    validate_auth,
    validate_provider,
)
from app.main import app
from app.models.git_remote import GitRemoteSettings
from app.services.credential_store import CredentialStoreUnavailableError, KeyringCredentialStore
from app.services.git_provider import ProviderClient
from app.services.git_remote import (
    GitRemoteError,
    GitRemoteService,
    GitResult,
    GitRunner,
    GitSettingsInput,
    SettingsNotFoundError,
    redact,
)
from app.services.patch import PatchNotFoundError
from app.tools import get_registry
from tests.test_patches import World, accepted, create
from tests.test_patches import world as _world_fixture

world = _world_fixture  # re-exported so pytest finds the fixture here

PAT = "ghp_SuperSecretToken1234567890"
APP_DIR = Path(__file__).resolve().parent.parent / "app"


class FakeStore:
    def __init__(self, available: bool = True) -> None:
        self.is_available = available
        self.items: dict[str, str] = {}

    def available(self) -> bool:
        return self.is_available

    def set(self, reference: str, secret: str) -> None:
        self.items[reference] = secret

    def get(self, reference: str) -> str | None:
        return self.items.get(reference)

    def delete(self, reference: str) -> None:
        self.items.pop(reference, None)


class NetFreeRunner(GitRunner):
    """Real git for local work; `ls-remote` is answered without touching any network."""

    def __init__(self, heads: bool = True) -> None:
        super().__init__(allow_local=True)
        self.heads = heads
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, args: Any, cwd: Path | None = None, token: str | None = None) -> GitResult:
        self.calls.append((list(args), token))
        if args[0] == "ls-remote":
            return GitResult(0, "abc123\trefs/heads/x\n" if self.heads else "", "")
        return super().run(args, cwd, token)


def bgit(bare: str, *args: str) -> str:
    return git(Path(bare).parent, "--git-dir", bare, *args)


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


@pytest.fixture
def bare(tmp_path: Path) -> str:
    """A local bare repository with one commit on main. Never a real remote."""
    seed, remote = tmp_path / "seed", tmp_path / "remote.git"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(seed, "push", "-q", str(remote), "main")
    return remote.as_posix()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    root = tmp_path / "ws"
    root.mkdir()
    return str(root)


def make(
    engine: Engine,
    workspace: str,
    store: Any,
    *,
    runner: GitRunner | None = None,
    local: bool = True,
    client: ProviderClient | None = None,
    own: list[Any] | None = None,
) -> GitRemoteService:
    return GitRemoteService(
        Session(engine, expire_on_commit=False),
        workspace_root=workspace,
        store=store,
        runner=runner,
        provider_client=client,
        allow_local=local,
        own_remotes=[] if own is None else own,
    )


def settings_in(url: str, **over: Any) -> GitSettingsInput:
    values: dict[str, Any] = {
        "provider": "generic",
        "remote_url": url,
        "clone_folder": None,
        "base_branch": "main",
        "branch_prefix": "agent/",
        "author_name": "Ana Dev",
        "author_email": "ana@example.com",
        "auth_method": "credential_manager",
    }
    values.update(over)
    return GitSettingsInput(**values)


class Ready:
    def __init__(self, svc: GitRemoteService, world: World, patch: dict[str, Any], clone: Path):
        self.svc, self.world, self.patch, self.clone = svc, world, patch, clone
        self.pid = uuid.UUID(world[0])
        self.patch_id = uuid.UUID(patch["id"])


@pytest.fixture
def ready(
    client: TestClient, engine: Engine, world: World, bare: str, workspace: str, store: FakeStore
) -> Ready:
    patch = accepted(client, world, [create("src/app.py", "x = 1\n")])
    svc = make(engine, workspace, store, runner=NetFreeRunner())
    pid = uuid.UUID(world[0])
    svc.save(pid, settings_in(bare))
    clone = svc.clone(pid)
    return Ready(svc, world, patch, clone)


# ---- settings validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "url", "auth"),
    [
        ("github", "https://github.com/octo/repo.git", "credential_manager"),
        ("github", "git@github.com:octo/repo.git", "ssh"),
        ("github_enterprise", "https://ghe.corp.example/octo/repo", "pat"),
        ("azure_devops", "https://dev.azure.com/org/proj/_git/repo", "pat"),
        ("azure_devops", "git@ssh.dev.azure.com:v3/org/proj/repo", "ssh"),
        ("gitlab", "https://gitlab.com/group/sub/project.git", "pat"),
        ("generic", "https://git.example.com/team/repo.git", "credential_manager"),
        ("generic", "ssh://git@git.example.com:2222/team/repo.git", "ssh"),
    ],
)
def test_valid_provider_settings(provider: str, url: str, auth: str) -> None:
    remote = parse_remote(url)
    validate_provider(provider, remote)
    validate_auth(auth, remote)


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("github", "https://gitlab.com/octo/repo"),
        ("github", "https://github.com/octo"),
        ("github_enterprise", "https://github.com/octo/repo"),
        ("azure_devops", "https://dev.azure.com/org/repo"),
        ("azure_devops", "https://github.com/octo/repo"),
        ("gitlab", "https://gitlab.com/onlyone"),
        ("nonsense", "https://git.example.com/a/b"),
    ],
)
def test_provider_url_mismatch_is_refused(provider: str, url: str) -> None:
    with pytest.raises(GitSettingsError):
        validate_provider(provider, parse_remote(url))


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r",
        "https://user:secret@github.com/o/r",
        "https://token@github.com/o/r",
        "file:///etc/passwd",
        "--upload-pack=evil",
        "ext::sh -c evil",
        "https://github.com/o/r?x=1",
        "/just/a/path",
        "",
    ],
)
def test_unsafe_remote_urls_are_refused(url: str) -> None:
    with pytest.raises(GitSettingsError):
        parse_remote(url)


def test_auth_method_must_fit_the_url() -> None:
    with pytest.raises(GitSettingsError):
        validate_auth("pat", parse_remote("git@github.com:o/r.git"))
    with pytest.raises(GitSettingsError):
        validate_auth("ssh", parse_remote("https://github.com/o/r"))
    with pytest.raises(GitSettingsError):
        validate_auth("password", parse_remote("https://github.com/o/r"))


def test_save_validates_and_defaults(engine: Engine, world: World, workspace: str) -> None:
    svc = make(engine, workspace, FakeStore(), local=False)
    pid = uuid.UUID(world[0])
    row = svc.save(pid, settings_in("https://git.example.com/team/repo.git"))
    assert (row.base_branch, row.branch_prefix, row.auth_method) == (
        "main",
        "agent/",
        "credential_manager",
    )
    assert row.clone_folder == f"git/{pid}"
    for bad in (
        {"base_branch": "bad branch"},
        {"base_branch": "a..b"},
        {"branch_prefix": "../x/"},
        {"author_email": "nope"},
        {"author_name": ""},
        {"clone_folder": "../escape"},
        {"clone_folder": "/abs/path"},
        {"clone_folder": "C:/abs"},
        {"clone_folder": str(uuid.uuid4())},
        {"clone_folder": "a/.git/b"},
    ):
        with pytest.raises(GitSettingsError):
            svc.save(pid, settings_in("https://git.example.com/team/repo.git", **bad))
    with pytest.raises(GitRemoteError) as info:
        make(engine, "", FakeStore(), local=False).save(
            pid, settings_in("https://git.example.com/team/repo.git")
        )
    assert info.value.code == "workspace_not_configured"


def test_workspace_root_inside_the_repository_is_refused(engine: Engine, world: World) -> None:
    inside = str(Path(__file__).resolve().parent)
    with pytest.raises(GitRemoteError) as info:
        make(engine, inside, FakeStore(), local=False).save(
            uuid.UUID(world[0]), settings_in("https://git.example.com/team/repo.git")
        )
    assert info.value.code == "workspace_location"


def test_archpilotai_repository_is_rejected(engine: Engine, world: World, workspace: str) -> None:
    svc = make(engine, workspace, FakeStore(), local=False, own=None)  # reads the real origin
    pid = uuid.UUID(world[0])
    for provider, url, auth in (
        ("github", "https://github.com/divanster/ArchPilotAI.git", "credential_manager"),
        ("github", "git@github.com:divanster/ArchPilotAI.git", "ssh"),
        ("github", "https://github.com/someone-else/archpilotai", "credential_manager"),
        ("generic", "https://mirror.example.com/x/ArchPilotAI.git", "credential_manager"),
    ):
        with pytest.raises(GitRemoteError) as info:
            svc.save(pid, settings_in(url, provider=provider, auth_method=auth))
        assert info.value.code == "own_repository"
    with Session(engine) as session:
        assert session.get(GitRemoteSettings, pid) is None


def test_attaching_a_clone_of_another_remote_is_refused(
    ready: Ready, tmp_path: Path, engine: Engine
) -> None:
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(other)], check=True)
    git(ready.clone, "remote", "set-url", "origin", other.as_posix())
    with pytest.raises(GitRemoteError) as info:
        ready.svc.attach(ready.pid)
    assert info.value.code == "remote_mismatch"


# ---- PAT handling --------------------------------------------------------------------------


def use_service(service: GitRemoteService) -> None:
    app.dependency_overrides[get_git_service] = lambda: service


def api(pid: str, suffix: str = "") -> str:
    return f"/api/projects/{pid}/git{suffix}"


BODY = {
    "provider": "github",
    "remote_url": "https://github.com/octo/repo.git",
    "author_name": "Ana Dev",
    "author_email": "ana@example.com",
    "auth_method": "pat",
}


def test_pat_is_stored_only_in_the_credential_store(
    client: TestClient,
    engine: Engine,
    world: World,
    workspace: str,
    store: FakeStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    pid = world[0]
    runner = NetFreeRunner()
    mock = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": 1}))
    svc = make(
        engine, workspace, store, runner=runner, client=ProviderClient(httpx.Client(transport=mock))
    )
    svc.__dict__["_allow_local"] = False
    use_service(svc)
    assert client.put(api(pid), json=BODY).status_code == 200
    saved = client.put(api(pid, "/pat"), json={"token": PAT})
    assert saved.status_code == 200, saved.text
    assert saved.json()["pat"] == {"saved": True, "provider": "github", "last4": PAT[-4:]}
    assert list(store.items.values()) == [PAT]

    responses = [
        saved,
        client.get(api(pid)),
        client.post(api(pid, "/test")),
        client.put(api(pid, "/pat"), json={"token": "short"}),
    ]
    assert responses[2].json()["api_ok"] is True
    assert responses[3].status_code == 422
    for response in responses:
        assert PAT not in response.text
        assert "short" not in response.text
    with engine.connect() as conn:
        dump = str(conn.execute(text("SELECT * FROM git_remote_settings")).all())
    assert PAT not in dump
    assert PAT not in caplog.text
    assert not any(PAT in arg for args, _ in runner.calls for arg in args)
    assert [t for _, t in runner.calls if t] == [PAT]  # only handed over as the token, for git
    assert (
        not (Path(__file__).resolve().parents[1] / ".env").read_text().count(PAT)
        if (Path(__file__).resolve().parents[1] / ".env").exists()
        else True
    )

    assert client.delete(api(pid, "/pat")).json()["pat"]["saved"] is False
    assert store.items == {}


def test_token_reaches_git_only_through_the_environment() -> None:
    runner = GitRunner()
    env = runner._env(PAT)
    assert env["ARCHPILOT_GIT_PAT"] == PAT
    assert PAT not in "".join(v for k, v in env.items() if k != "ARCHPILOT_GIT_PAT")
    assert "ARCHPILOT_GIT_PAT" not in runner._env(None)
    done = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=example.com\n\n",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert f"password={PAT}" in done.stdout


def test_error_text_is_redacted() -> None:
    text_ = f"fatal: unable to access 'https://user:{PAT}@host/x.git/': bad {PAT}"
    cleaned = redact(text_, PAT)
    assert PAT not in cleaned
    assert "user:" not in cleaned


def test_unavailable_keyring_refuses_to_save_the_pat(
    client: TestClient, engine: Engine, world: World, workspace: str
) -> None:
    store = FakeStore(available=False)
    use_service(make(engine, workspace, store, local=False))
    assert client.put(api(world[0]), json=BODY).status_code == 200
    refused = client.put(api(world[0], "/pat"), json={"token": PAT})
    assert refused.status_code == 409
    assert "credential store" in refused.json()["detail"]
    assert PAT not in refused.text
    assert client.get(api(world[0])).json()["pat"]["saved"] is False
    assert store.items == {}


def test_real_keyring_without_backend_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    from keyring.backends import fail

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())  # type: ignore[no-untyped-call]
    store = KeyringCredentialStore()
    assert store.available() is False
    with pytest.raises(CredentialStoreUnavailableError):
        store.set("ref", PAT)


def test_pat_is_dropped_when_the_remote_changes(
    engine: Engine, world: World, workspace: str, store: FakeStore
) -> None:
    svc = make(engine, workspace, store, local=False)
    pid = uuid.UUID(world[0])
    svc.save(pid, settings_in("https://git.example.com/a/one.git"))
    svc.save_pat(pid, PAT)
    assert len(store.items) == 1
    row = svc.save(pid, settings_in("https://git.example.com/a/two.git"))
    assert store.items == {} and row.pat_ref is None and row.pat_last4 is None


# ---- connection ------------------------------------------------------------------------------


def test_connection_test_uses_ls_remote(ready: Ready, bare: str, tmp_path: Path) -> None:
    result = ready.svc.test_connection(ready.pid)
    assert result.reachable and not result.api_checked
    row = ready.svc.get(ready.pid)
    row.remote_url = (tmp_path / "missing.git").as_posix()
    ready.svc._session.commit()
    ready.svc._git = GitRunner(allow_local=True)  # real git: a missing path is unreachable
    assert ready.svc.test_connection(ready.pid).reachable is False


@pytest.mark.parametrize("status_code", [200, 401])
def test_connection_test_checks_the_provider_with_a_pat(
    engine: Engine, world: World, workspace: str, store: FakeStore, status_code: int
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, json={})

    svc = make(
        engine,
        workspace,
        store,
        runner=NetFreeRunner(),
        client=ProviderClient(httpx.Client(transport=httpx.MockTransport(handler))),
        local=False,
    )
    pid = uuid.UUID(world[0])
    svc.save(pid, settings_in("https://github.com/octo/repo.git", provider="github"))
    assert svc.test_connection(pid).api_checked is False
    svc.save_pat(pid, PAT)
    result = svc.test_connection(pid)
    assert result.api_checked and result.api_ok is (status_code == 200)
    assert [r.method for r in seen] == ["GET"]
    assert PAT not in result.message


# ---- commit ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "key", "title", "expected"),
    [
        ("agent/", "T-001", "Add the Login page!", "agent/T-001-add-the-login-page"),
        ("", "T-2", "  ", "T-2-task"),
        ("bot/", "T 3", "Ünïcode & Symbols", "bot/T-3-n-code-symbols"),
        ("agent/", "T-9", "x" * 100, "agent/T-9-" + "x" * 40),
    ],
)
def test_branch_naming(prefix: str, key: str, title: str, expected: str) -> None:
    assert branch_name(prefix, key, title) == expected


def test_commit_creates_the_branch_and_names_task_version_reviewer(ready: Ready) -> None:
    result = ready.svc.commit_patch(ready.pid, ready.patch_id)
    key = ready.patch["task_key"]
    assert result.created_branch and result.branch.startswith(f"agent/{key}-")
    assert (ready.clone / "src" / "app.py").read_text() == "x = 1\n"
    message = git(ready.clone, "log", "-1", "--format=%B", result.branch)
    assert key in message and "Patch: v1" in message and "Reviewer: ana" in message
    assert (
        git(ready.clone, "log", "-1", "--format=%an <%ae>").strip() == "Ana Dev <ana@example.com>"
    )
    assert git(ready.clone, "rev-parse", "main").strip() != result.commit  # base untouched


def test_commit_reuses_the_branch_and_is_idempotent(ready: Ready) -> None:
    first = ready.svc.commit_patch(ready.pid, ready.patch_id)
    git(ready.clone, "checkout", "-q", "main")
    with pytest.raises(GitRemoteError) as info:
        ready.svc.commit_patch(ready.pid, ready.patch_id)
    assert info.value.code == "nothing_to_commit"
    assert git(ready.clone, "rev-list", "--count", f"main..{first.branch}").strip() == "1"
    assert git(ready.clone, "branch", "--show-current").strip() == "main"


def test_dirty_tree_is_refused(ready: Ready) -> None:
    (ready.clone / "stray.txt").write_text("not mine\n")
    with pytest.raises(GitRemoteError) as info:
        ready.svc.commit_patch(ready.pid, ready.patch_id)
    assert info.value.code == "dirty_tree"
    assert git(ready.clone, "branch", "--list", "agent/*").strip() == ""
    assert not (ready.clone / "src").exists()


def test_foreign_commits_on_the_branch_are_refused(ready: Ready) -> None:
    key = ready.patch["task_key"]
    task_branch = git(ready.clone, "branch", "--show-current")
    assert task_branch.strip() == "main"
    probe = ready.svc.commit_patch(ready.pid, ready.patch_id)
    git(ready.clone, "reset", "-q", "--hard", "main")  # drop our commit, keep the branch name
    (ready.clone / "hand.txt").write_text("by a person\n")
    git(ready.clone, "add", ".")
    git(ready.clone, "commit", "-q", "-m", "manual change")
    manual = git(ready.clone, "rev-parse", "HEAD").strip()
    git(ready.clone, "checkout", "-q", "main")
    git(ready.clone, "branch", "-f", probe.branch, manual)
    with pytest.raises(GitRemoteError) as info:
        ready.svc.commit_patch(ready.pid, ready.patch_id)
    assert info.value.code == "foreign_commits" and key in probe.branch
    assert git(ready.clone, "branch", "--show-current").strip() == "main"


def test_an_unaccepted_patch_is_not_committed(
    client: TestClient, ready: Ready, world: World
) -> None:
    from tests.test_patches import generate, serve

    other = list(world[2])[-1]
    serve([create("other.py")])
    created = generate(client, world, other)
    assert created.status_code == 201, created.text
    with pytest.raises(GitRemoteError) as info:
        ready.svc.commit_patch(ready.pid, uuid.UUID(created.json()["id"]))
    assert info.value.code == "patch_not_accepted"


def test_unsafe_patch_paths_never_leave_the_clone(ready: Ready) -> None:
    from app.models.patch import PatchFile

    session = ready.svc._session
    session.add(
        PatchFile(
            patch_id=ready.patch_id,
            project_id=ready.pid,
            path=".git/config",
            change="modify",
            content="x",
        )
    )
    session.commit()
    with pytest.raises(GitRemoteError) as info:
        ready.svc.commit_patch(ready.pid, ready.patch_id)
    assert info.value.code == "invalid_path"


# ---- push --------------------------------------------------------------------------------------


def test_push_sends_only_the_work_branch(ready: Ready, bare: str) -> None:
    result = ready.svc.commit_patch(ready.pid, ready.patch_id)
    before = bgit(bare, "rev-parse", "main").strip()
    assert ready.svc.push(ready.pid, result.branch, "ana") == result.branch
    assert bgit(bare, "rev-parse", result.branch).strip() == result.commit
    assert bgit(bare, "rev-parse", "main").strip() == before


def test_push_to_base_or_default_branch_is_refused(ready: Ready, bare: str) -> None:
    result = ready.svc.commit_patch(ready.pid, ready.patch_id)
    before = bgit(bare, "rev-parse", "main").strip()
    for name in ("main", "MAIN", "master"):
        with pytest.raises(GitRemoteError) as info:
            ready.svc.push(ready.pid, name, "ana")
        assert info.value.code == "protected_branch"
    row = ready.svc.get(ready.pid)
    row.base_branch = "release"
    ready.svc._session.commit()
    with pytest.raises(GitRemoteError) as info:
        ready.svc.push(ready.pid, "release", "ana")
    assert info.value.code == "protected_branch"
    row.base_branch = "main"
    row.branch_prefix = "other/"
    ready.svc._session.commit()
    with pytest.raises(GitRemoteError):
        ready.svc.push(ready.pid, result.branch, "ana")  # outside the configured prefix
    assert bgit(bare, "rev-parse", "main").strip() == before


def test_push_endpoint_needs_confirmation_and_a_named_approver(
    client: TestClient, ready: Ready, bare: str
) -> None:
    use_service(ready.svc)
    branch = ready.svc.commit_patch(ready.pid, ready.patch_id).branch
    url = api(ready.world[0], "/push")
    assert client.post(url, json={"branch": branch, "approved_by": "ana"}).status_code == 409
    assert (
        client.post(url, json={"branch": "main", "approved_by": "ana", "confirm": True}).status_code
        == 409
    )
    assert (
        client.post(url, json={"branch": branch, "approved_by": " ", "confirm": True}).status_code
        == 422
    )
    ok = client.post(url, json={"branch": branch, "approved_by": "ana", "confirm": True})
    assert ok.status_code == 200 and ok.json() == {"branch": branch, "pushed": True}


def test_push_is_not_reachable_by_the_agent() -> None:
    registry = get_registry()
    for tool in registry.all():
        lowered = f"{tool.name} {tool.description}".lower()
        assert "push" not in lowered and "git_remote" not in lowered
        assert "pull request" not in lowered and "commit" not in lowered
    for audience in ("mcp", "agent"):
        for name in ("push", "git_push", "commit_patch", "create_pull_request"):
            assert registry.find(audience, name) is None
    guarded = [
        *(APP_DIR / "mcp").rglob("*.py"),
        *(APP_DIR / "tools").rglob("*.py"),
        *(APP_DIR / "llm").rglob("*.py"),
        *APP_DIR.joinpath("services").glob("agent*.py"),
        APP_DIR / "domain" / "agent.py",
    ]
    for path in guarded:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if isinstance(node, ast.Import | ast.ImportFrom)
                else []
            )
            assert not any("git_remote" in n or "git_provider" in n for n in names), path


# ---- pull request ----------------------------------------------------------------------------


PROVIDERS = [
    (
        "github",
        "https://github.com/octo/repo.git",
        "https://api.github.com/repos/octo/repo/pulls",
        "https://github.com/octo/repo/compare/main...",
    ),
    (
        "github_enterprise",
        "https://ghe.corp.example/octo/repo.git",
        "https://ghe.corp.example/api/v3/repos/octo/repo/pulls",
        "https://ghe.corp.example/octo/repo/compare/main...",
    ),
    (
        "gitlab",
        "https://gitlab.example.com/grp/sub/proj.git",
        "https://gitlab.example.com/api/v4/projects/grp%2Fsub%2Fproj/merge_requests",
        "https://gitlab.example.com/grp/sub/proj/-/merge_requests/new?",
    ),
    (
        "azure_devops",
        "https://dev.azure.com/org/proj/_git/repo",
        "https://dev.azure.com/org/proj/_apis/git/repositories/repo/pullrequests",
        "https://dev.azure.com/org/proj/_git/repo/pullrequestcreate?",
    ),
]


def retarget(ready: Ready, provider: str, url: str, auth: str = "credential_manager") -> None:
    """Point the settings and the clone's origin at a provider URL (the runner is network-free)."""
    row = ready.svc.get(ready.pid)
    row.provider, row.remote_url, row.auth_method = provider, url, auth
    ready.svc._session.commit()
    git(ready.clone, "remote", "set-url", "origin", url)
    ready.svc._allow_local = False


def mocked(seen: list[httpx.Request], reply: dict[str, Any]) -> ProviderClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=reply)

    return ProviderClient(httpx.Client(transport=httpx.MockTransport(handler)))


REPLIES: dict[str, dict[str, Any]] = {
    "github": {"html_url": "https://github.com/octo/repo/pull/7", "number": 7},
    "github_enterprise": {"html_url": "https://ghe.corp.example/octo/repo/pull/7", "number": 7},
    "gitlab": {"web_url": "https://gitlab.example.com/grp/sub/proj/-/merge_requests/7", "iid": 7},
    "azure_devops": {"pullRequestId": 7},
}


@pytest.mark.parametrize(("provider", "url", "api_url", "link"), PROVIDERS)
def test_pull_request_with_a_pat_goes_through_the_provider_api(
    ready: Ready, provider: str, url: str, api_url: str, link: str
) -> None:
    ready.svc.commit_patch(ready.pid, ready.patch_id)
    seen: list[httpx.Request] = []
    ready.svc._provider = mocked(seen, REPLIES[provider])
    retarget(ready, provider, url, "pat")
    ready.svc.save_pat(ready.pid, PAT)
    outcome = ready.svc.pull_request(ready.pid, ready.patch_id)
    assert outcome.mode == "created" and outcome.url and outcome.url.startswith("https://")
    assert len(seen) == 1 and seen[0].method == "POST"
    assert str(seen[0].url).split("?")[0] == api_url
    sent = seen[0].content.decode()
    assert ready.patch["task_key"] in sent and "Reviewer" not in sent.replace("accepted by", "")
    assert "accepted by ana" in sent and not str(seen[0].url).rstrip("/").endswith("/merge")
    assert PAT not in sent and PAT not in str(seen[0].url) and PAT not in outcome.message
    assert "ghp_" not in str(outcome)
    auth = {k.lower(): v for k, v in seen[0].headers.items()}
    assert PAT in str(auth.get("authorization", "")) + str(auth.get("private-token", "")) or (
        provider == "azure_devops" and "authorization" in auth
    )


@pytest.mark.parametrize(("provider", "url", "api_url", "link"), PROVIDERS)
def test_pull_request_without_a_pat_returns_a_compare_link(
    ready: Ready, provider: str, url: str, api_url: str, link: str
) -> None:
    result = ready.svc.commit_patch(ready.pid, ready.patch_id)
    seen: list[httpx.Request] = []
    ready.svc._provider = mocked(seen, {})
    retarget(ready, provider, url)
    outcome = ready.svc.pull_request(ready.pid, ready.patch_id)
    assert outcome.mode == "compare_link" and seen == []
    assert outcome.url is not None and outcome.url.startswith(link)
    assert result.branch.replace("/", "%2F") in outcome.url or result.branch in outcome.url


def test_generic_provider_has_no_compare_link(ready: Ready) -> None:
    ready.svc.commit_patch(ready.pid, ready.patch_id)
    retarget(ready, "generic", "https://git.example.com/team/repo.git")
    outcome = ready.svc.pull_request(ready.pid, ready.patch_id)
    assert outcome.mode == "compare_link" and outcome.url is None


def test_pull_request_needs_a_pushed_branch_and_never_merges(
    ready: Ready,
) -> None:
    with pytest.raises(GitRemoteError) as info:
        ready.svc.pull_request(ready.pid, ready.patch_id)
    assert info.value.code == "branch_not_found"
    ready.svc.commit_patch(ready.pid, ready.patch_id)
    ready.svc._git = NetFreeRunner(heads=False)
    with pytest.raises(GitRemoteError) as info:
        ready.svc.pull_request(ready.pid, ready.patch_id)
    assert info.value.code == "not_pushed"
    source = (APP_DIR / "services" / "git_provider.py").read_text(encoding="utf-8").lower()
    assert not re.search(r"/merge(?!_requests)", source) and ".put(" not in source


def test_provider_failure_is_reported_without_the_body(ready: Ready) -> None:
    ready.svc.commit_patch(ready.pid, ready.patch_id)
    handler = httpx.MockTransport(lambda r: httpx.Response(403, text=f"denied {PAT}"))
    ready.svc._provider = ProviderClient(httpx.Client(transport=handler))
    retarget(ready, "github", "https://github.com/octo/repo.git", "pat")
    ready.svc.save_pat(ready.pid, PAT)
    with pytest.raises(GitRemoteError) as info:
        ready.svc.pull_request(ready.pid, ready.patch_id)
    assert info.value.code == "provider_failed" and PAT not in str(info.value)
    assert "403" in str(info.value)


def test_compare_link_builder_directly() -> None:
    gh = parse_remote("git@github.com:octo/repo.git")
    assert compare_url("github", gh, "main", "agent/T-1-x") == (
        "https://github.com/octo/repo/compare/main...agent/T-1-x?expand=1"
    )
    assert compare_url("generic", gh, "main", "x") is None


# ---- isolation --------------------------------------------------------------------------------


def test_projects_are_isolated(
    client: TestClient, ready: Ready, engine: Engine, workspace: str, store: FakeStore, bare: str
) -> None:
    other_id = uuid.UUID(client.post("/api/projects", json={"name": "other"}).json()["id"])
    svc = make(engine, workspace, store, runner=NetFreeRunner())
    with pytest.raises(SettingsNotFoundError):
        svc.get(other_id)
    # the other project cannot use this project's folder, nor commit this project's patch
    with pytest.raises(GitRemoteError) as info:
        svc.save(other_id, settings_in(bare, clone_folder=ready.svc.get(ready.pid).clone_folder))
    assert info.value.code == "clone_folder_taken"
    svc.save(other_id, settings_in(bare, clone_folder="git/other"))
    svc.clone(other_id)
    with pytest.raises(PatchNotFoundError):
        svc.commit_patch(other_id, ready.patch_id)
    # tokens are per project
    ready.svc.save_pat(ready.pid, PAT)
    assert svc.get(other_id).pat_ref is None
    svc.delete(other_id)
    assert ready.svc.get(ready.pid).pat_last4 == PAT[-4:]
    assert list(store.items) == [ready.svc.get(ready.pid).pat_ref]


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_git_service, None)
