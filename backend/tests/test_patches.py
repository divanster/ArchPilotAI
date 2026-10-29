import ast
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, create_engine, func, inspect, select, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.api.patches import (
    PatchResponse,
    RemovalItemOut,
    RemovalMissingOut,
    RemovalsNotConfirmedBody,
)
from app.core.config import Settings, get_settings
from app.domain.plan import AI_RECOMMENDATION_FLAG
from app.llm.fake_provider import FakeLLMProvider
from app.mcp import tools
from app.mcp.tools import ToolContext, ToolInputError
from app.models.patch import CodePatch, PatchFile, WorkspaceFile
from app.models.plan import PlanTask
from app.services.workspace_export import MANIFEST_NAME, REPO_ROOT
from tests.conftest import alembic_config
from tests.contract_fixtures import FIXTURE_DIR, FIXTURES, render
from tests.test_plans import approved_project, build, plans_url
from tests.test_requirements_api import use

World = tuple[str, dict[str, Any], dict[str, Any]]  # project id, plan, tasks by key


def api(pid: str, suffix: str) -> str:
    return f"/api/projects/{pid}{suffix}"


def task_url(pid: str, plan_id: str, key: str, suffix: str) -> str:
    return api(pid, f"/plans/{plan_id}/tasks/{key}{suffix}")


def serve(files: list[dict[str, str]], **extra: Any) -> FakeLLMProvider:
    provider = FakeLLMProvider(patch_text=json.dumps({"files": files, **extra}))
    use(provider)
    return provider


def create(path: str, content: str = "x = 1\n") -> dict[str, str]:
    return {"path": path, "change": "create", "content": content}


def generate(client: TestClient, world: World, key: str | None = None, **body: Any) -> Response:
    pid, plan, tasks = world
    key = key or next(iter(tasks))
    response: Response = client.post(task_url(pid, plan["id"], key, "/patches"), json=body)
    return response


def review(client: TestClient, pid: str, patch_id: str, action: str = "accept") -> Response:
    response: Response = client.post(
        api(pid, f"/patches/{patch_id}/{action}"), json={"reviewer": "ana", "reason": "looks fine"}
    )
    return response


def accepted(client: TestClient, world: World, files: list[dict[str, str]]) -> dict[str, Any]:
    serve(files)
    created = generate(client, world)
    assert created.status_code == 201, created.text
    done = review(client, world[0], created.json()["id"])
    assert done.status_code == 200, done.text
    body: dict[str, Any] = done.json()
    return body


def patch_count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(CodePatch)) or 0


def set_task(engine: Engine, task_id: str, **values: Any) -> None:
    with Session(engine) as session:
        session.execute(update(PlanTask).where(PlanTask.id == uuid.UUID(task_id)).values(**values))
        session.commit()


def limits(monkeypatch: pytest.MonkeyPatch, **changes: int) -> None:
    patched = get_settings().model_copy(update={f"patch_{k}": v for k, v in changes.items()})
    monkeypatch.setattr("app.api.deps.get_settings", lambda: patched)


def workspace_root(monkeypatch: pytest.MonkeyPatch, root: str) -> None:
    patched: Settings = get_settings().model_copy(update={"workspace_root": root})
    monkeypatch.setattr("app.api.patches.get_settings", lambda: patched)


@pytest.fixture
def world(client: TestClient, engine: Engine) -> World:
    pid, _proposal, _ids = approved_project(client)
    plan = build(client, pid)
    assert client.post(plans_url(pid, f"/{plan['id']}/approve")).status_code == 200
    tasks = {t["key"]: t for t in plan["tasks"]}
    for task in tasks.values():  # gates are set explicitly per test
        set_task(engine, task["id"], flags=[], blocking_questions=[])
    return pid, plan, tasks


@pytest.fixture
def mcp_ctx(engine: Engine) -> ToolContext:
    return ToolContext(sessionmaker(bind=engine, expire_on_commit=False))


# ---- gates -----------------------------------------------------------------------------------


def test_generation_proposes_a_patch_without_touching_the_workspace(
    client: TestClient, engine: Engine, world: World
) -> None:
    serve([create("src/app.py", "def run() -> int:\n    return 1\n")], notes=["n"])
    response = generate(client, world)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["status"], body["version"], body["validation"]) == ("proposed", 1, "passed")
    assert body["prompt_version"] == "patch-v1" and body["notes"] == ["n"]
    assert [f["path"] for f in body["files"]] == ["src/app.py"]
    assert "+def run() -> int:" in body["files"][0]["diff"]
    assert client.get(api(world[0], "/workspace")).json() == {"files": []}


def test_unapproved_plan_is_refused(client: TestClient, engine: Engine, world: World) -> None:
    pid, _plan, _tasks = world
    draft = build(client, pid)
    key = draft["tasks"][0]["key"]
    serve([create("src/a.py")])
    response = client.post(task_url(pid, draft["id"], key, "/patches"), json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "plan_not_approved"
    assert patch_count(engine) == 0


def test_unconfirmed_ai_technology_blocks_until_a_person_confirms(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid, plan, tasks = world
    key, task = next(iter(tasks.items()))
    set_task(engine, task["id"], flags=[AI_RECOMMENDATION_FLAG])
    serve([create("src/a.py")])
    blocked = generate(client, world)
    assert (
        blocked.status_code == 409 and blocked.json()["detail"]["code"] == "technology_unconfirmed"
    )
    url = task_url(pid, plan["id"], key, "/technology-confirmation")
    assert client.post(url, json={"name": "", "reason": "r"}).status_code == 422
    ok = client.post(url, json={"name": "Ana", "reason": "Postgres is mandated"})
    assert ok.status_code == 201 and ok.json()["name"] == "Ana"
    assert client.get(url).json()[0]["reason"] == "Postgres is mandated"
    assert generate(client, world).status_code == 201


def test_confirming_an_unflagged_task_is_refused(client: TestClient, world: World) -> None:
    pid, plan, tasks = world
    key = next(iter(tasks))
    url = task_url(pid, plan["id"], key, "/technology-confirmation")
    assert client.post(url, json={"name": "Ana", "reason": "r"}).status_code == 409


def test_blocking_questions_need_explicit_acknowledgement(
    client: TestClient, engine: Engine, world: World
) -> None:
    _pid, _plan, tasks = world
    set_task(engine, next(iter(tasks.values()))["id"], blocking_questions=["Which region?"])
    serve([create("src/a.py")])
    blocked = generate(client, world)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "blocking_questions"
    assert patch_count(engine) == 0
    allowed = generate(client, world, acknowledge_blocking_questions=True)
    assert allowed.status_code == 201 and allowed.json()["blocking_acknowledged"] is True


def test_dependencies_without_accepted_patch_only_warn(client: TestClient, world: World) -> None:
    _pid, _plan, tasks = world
    key = next(k for k, t in tasks.items() if t["depends_on"])
    serve([create("src/a.py")])
    response = generate(client, world, key)
    assert response.status_code == 201
    assert any("no accepted patch" in w for w in response.json()["warnings"])


def test_unknown_task_and_project_are_404(client: TestClient, world: World) -> None:
    pid, plan, _tasks = world
    assert client.post(task_url(pid, plan["id"], "T-999", "/patches"), json={}).status_code == 404
    other = "00000000-0000-4000-8000-000000000000"
    assert client.get(api(other, "/workspace")).status_code == 404


# ---- validation ------------------------------------------------------------------------------

BAD_FILES = [
    (create("../x.py"), "path_traversal"),
    (create("src/../../x.py"), "path_traversal"),
    (create("/etc/x.py"), "path_absolute"),
    (create("\\x.py"), "path_absolute"),
    (create("C:/x.py"), "path_drive"),
    (create("C:\\x.py"), "path_drive"),
    (create(".hidden/x.py"), "path_hidden"),
    (create(".env"), "path_hidden"),
    (create("src/x.exe", "x"), "extension_not_allowed"),
    (create("src/noext", "x"), "extension_not_allowed"),
    (create("src/a.txt", "a\x00b"), "not_text"),
    (create("src/a.txt", "ab\ud800"), "not_text"),
    (create("src/c.py", 'API_KEY = "abcd1234efgh5678ijkl"\n'), "secret_detected"),
    (create("src/c.py", 'password = "hunter2hunter2"\n'), "secret_detected"),
    (create("src/c.txt", "-----BEGIN RSA PRIVATE KEY-----\nabc\n"), "secret_detected"),
    (create("src/c.md", "use postgresql://admin:s3cr3tpass@db.internal/app\n"), "secret_detected"),
    (create("src/c.txt", "token ghp_abcdefghijklmnopqrstuvwxyz0123\n"), "secret_detected"),
    (create(".env.example", "DATABASE_URL=something\n"), "env_example_value"),
    (create("src/a.py", "def (:\n"), "python_syntax"),
    (create("src/a.json", "{bad"), "json_invalid"),
    (create("src/a.toml", "a = = 1"), "toml_invalid"),
    (create("src/a.yaml", "a: [1"), "yaml_invalid"),
    ({"path": "src/none.py", "change": "modify", "content": "x = 1\n"}, "target_missing"),
    ({"path": "src/none.py", "change": "delete", "content": ""}, "target_missing"),
    ({"path": "src/a.py", "change": "rename", "content": ""}, "invalid_change"),
]


@pytest.mark.parametrize(
    ("bad", "code"), BAD_FILES, ids=[c + str(i) for i, (_, c) in enumerate(BAD_FILES)]
)
def test_invalid_patches_are_rejected_whole_and_never_echoed(
    client: TestClient,
    engine: Engine,
    world: World,
    caplog: pytest.LogCaptureFixture,
    bad: dict[str, str],
    code: str,
) -> None:
    serve([create("src/fine.py"), bad])
    with caplog.at_level(logging.DEBUG):
        response = generate(client, world)
    assert response.status_code == 422, response.text
    failures = response.json()["detail"]["failures"]
    assert code in {f["code"] for f in failures}
    assert all(f["path"] != "src/fine.py" for f in failures)
    assert patch_count(engine) == 0
    secret = bad["content"].strip()
    if len(secret) > 3 and bad["change"] == "create":
        assert secret not in response.text and secret not in caplog.text


def test_valid_special_files_are_accepted(client: TestClient, world: World) -> None:
    serve(
        [
            create(".gitignore", "node_modules\n"),
            create(".env.example", "# keys\nAPI_KEY=\nDB_URL=\n"),
            create("cfg/a.json", '{"a": 1}'),
            create("cfg/a.toml", "a = 1\n"),
            create("cfg/a.yml", "a:\n  - 1\n"),
            create("web/app.ts", "export const a: number = 1;\nlet = = broken in ts\n"),
            create("README.md", "# Title\n"),
        ]
    )
    response = generate(client, world)
    assert response.status_code == 201, response.text


def test_size_and_count_caps(
    client: TestClient, engine: Engine, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve([create("src/a.py"), create("src/b.py")])
    limits(monkeypatch, max_files=1)
    response = generate(client, world)
    assert response.status_code == 422
    assert response.json()["detail"]["failures"] == [{"code": "too_many_files", "path": None}]

    serve([create("src/a.py", "x = '" + "a" * 200 + "'\n")])
    limits(monkeypatch, max_file_bytes=100)
    response = generate(client, world)
    assert [f["code"] for f in response.json()["detail"]["failures"]] == ["file_too_large"]

    serve([create("src/a.py", "x = 1\n" * 10), create("src/b.py", "x = 1\n" * 10)])
    limits(monkeypatch, max_patch_bytes=100)
    response = generate(client, world)
    assert [f["code"] for f in response.json()["detail"]["failures"]] == ["patch_too_large"]
    assert patch_count(engine) == 0


@pytest.mark.parametrize("raw", ["not json", "[]", '{"files": "x"}', '{"files": [], "extra": 1}'])
def test_malformed_model_output_is_422_without_echo(
    client: TestClient, world: World, raw: str
) -> None:
    use(FakeLLMProvider(patch_text=raw))
    response = generate(client, world)
    assert response.status_code == 422
    assert raw not in response.text or raw == "[]"


def test_duplicate_and_empty_patches_fail(client: TestClient, world: World) -> None:
    serve([create("src/a.py"), create("SRC/A.py")])
    assert "duplicate_path" in generate(client, world).text
    serve([])
    assert "empty_patch" in generate(client, world).text


# ---- review ----------------------------------------------------------------------------------


def test_accept_writes_the_workspace_and_updates_task_status(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid, plan, tasks = world
    key = next(iter(tasks))

    def statuses() -> dict[str, str]:
        found = client.get(plans_url(pid, f"/{plan['id']}")).json()["tasks"]
        return {t["key"]: t["status"] for t in found}

    assert set(statuses().values()) == {"todo"}
    serve([create("src/a.py", "a = 1\n")])
    created = generate(client, world).json()
    assert statuses()[key] == "proposed"
    done = review(client, pid, created["id"])
    assert done.status_code == 200
    assert (done.json()["status"], done.json()["reviewed_by"]) == ("accepted", "ana")
    assert done.json()["review_reason"] == "looks fine"
    assert statuses()[key] == "accepted"
    tree = client.get(api(pid, "/workspace")).json()["files"]
    assert [f["path"] for f in tree] == ["src/a.py"]
    with Session(engine) as session:
        row = session.scalars(select(WorkspaceFile)).one()
        assert row.content == "a = 1\n" and row.source_patch_id.hex == created["id"].replace(
            "-", ""
        )


def test_reject_leaves_the_workspace_alone(client: TestClient, world: World) -> None:
    pid, plan, tasks = world
    serve([create("src/a.py")])
    created = generate(client, world).json()
    rejected = review(client, pid, created["id"], "reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert client.get(api(pid, "/workspace")).json() == {"files": []}
    tasks_now = client.get(plans_url(pid, f"/{plan['id']}")).json()["tasks"]
    assert {t["status"] for t in tasks_now} == {"todo"}


def test_review_needs_name_and_reason(client: TestClient, world: World) -> None:
    serve([create("src/a.py")])
    created = generate(client, world).json()
    url = api(world[0], f"/patches/{created['id']}/accept")
    assert client.post(url, json={"reviewer": "", "reason": "x"}).status_code == 422
    assert client.post(url, json={"reviewer": "a", "reason": "  "}).status_code == 422
    assert client.get(api(world[0], f"/patches/{created['id']}")).json()["status"] == "proposed"


def test_reviewed_patches_are_immutable(client: TestClient, engine: Engine, world: World) -> None:
    pid = world[0]
    first = accepted(client, world, [create("src/a.py", "a = 1\n")])
    assert review(client, pid, first["id"]).status_code == 409
    assert review(client, pid, first["id"], "reject").status_code == 409
    serve([create("src/b.py")])
    second = generate(client, world).json()
    assert review(client, pid, second["id"], "reject").status_code == 200
    assert review(client, pid, second["id"]).status_code == 409
    again = client.get(api(pid, f"/patches/{second['id']}")).json()
    assert again["status"] == "rejected"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(WorkspaceFile)) == 1


def test_regenerate_creates_new_versions_and_keeps_old_ones(
    client: TestClient, world: World
) -> None:
    pid, plan, tasks = world
    key = next(iter(tasks))
    use(FakeLLMProvider())
    v1 = generate(client, world)
    assert v1.status_code == 201, v1.text
    assert review(client, pid, v1.json()["id"]).status_code == 200
    v2 = generate(client, world)
    assert v2.json()["version"] == 2 and v2.json()["files"][0]["change"] == "modify"
    assert v2.json()["files"][0]["previous_content_hash"]
    assert "+def revised_" in v2.json()["files"][0]["diff"]
    assert review(client, pid, v2.json()["id"]).status_code == 200
    listing = client.get(task_url(pid, plan["id"], key, "/patches")).json()
    assert sorted((p["version"], p["status"]) for p in listing) == [
        (1, "accepted"),
        (2, "accepted"),
    ]
    old = client.get(api(pid, f"/patches/{v1.json()['id']}")).json()
    assert old["files"][0]["content"] == v1.json()["files"][0]["content"]
    content = client.get(api(pid, "/workspace")).json()["files"]
    assert len(content) == 1


def test_stale_patch_cannot_be_accepted(client: TestClient, world: World) -> None:
    pid = world[0]
    use(FakeLLMProvider())
    base = generate(client, world).json()
    assert review(client, pid, base["id"]).status_code == 200
    a = generate(client, world).json()
    b = generate(client, world).json()
    assert review(client, pid, a["id"]).status_code == 200
    stale = client.get(api(pid, f"/patches/{b['id']}")).json()
    assert stale["validation"] == "stale"
    assert stale["stale"][0]["code"] == "stale_previous_hash"
    refused = review(client, pid, b["id"])
    assert refused.status_code == 409 and "stale_previous_hash" in refused.text
    assert client.get(api(pid, f"/patches/{b['id']}")).json()["status"] == "proposed"


def test_delete_removes_the_file_from_the_workspace(client: TestClient, world: World) -> None:
    pid = world[0]
    accepted(client, world, [create("src/a.py"), create("src/b.py")])
    accepted(client, world, [{"path": "src/a.py", "change": "delete", "content": ""}])
    paths = [f["path"] for f in client.get(api(pid, "/workspace")).json()["files"]]
    assert paths == ["src/b.py"]


def test_create_over_an_existing_file_becomes_modify_with_a_diff(
    client: TestClient, world: World
) -> None:
    accepted(client, world, [create("src/a.py", "x = 1\n")])
    serve([create("src/a.py", "y = 2\n")])
    response = generate(client, world)
    assert response.status_code == 201, response.text
    (file,) = response.json()["files"]
    assert file["change"] == "modify"
    assert "-x = 1" in file["diff"] and "+y = 2" in file["diff"]
    assert any("Changed create to modify" in n for n in response.json()["notes"])


def test_create_over_an_existing_path_that_cannot_be_matched_is_rejected(
    client: TestClient, engine: Engine, world: World
) -> None:
    accepted(client, world, [create("src/a.py")])
    before = patch_count(engine)
    serve([create("src/A.py", "y = 2\n")])
    response = generate(client, world)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "create_exists" in response.text and "use modify" in detail["message"]
    assert "src/A.py" in detail["message"]
    assert patch_count(engine) == before
    assert client.get(api(world[0], "/workspace")).json()["files"][0]["path"] == "src/a.py"


def test_new_file_is_labelled_create_with_a_full_diff(client: TestClient, world: World) -> None:
    serve([create("src/new.py", "z = 3\n")])
    (file,) = generate(client, world).json()["files"]
    assert file["change"] == "create" and "+z = 3" in file["diff"]


def test_existing_accepted_files_are_sent_to_the_model(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid, _plan, tasks = world
    first, second = list(tasks)[:2]
    accepted(client, world, [create("src/models.py", "class User:\n    pass\n")])
    set_task(engine, tasks[second]["id"], description="Add a field to models.py")
    provider = serve([create("src/other.py")])
    assert generate(client, world, second).status_code == 201
    prompt = provider.requests[-1].prompt
    assert "existing accepted files" in prompt
    assert "class User:" in prompt and '"src/models.py"' in prompt
    assert first != second


def test_other_projects_files_are_not_sent_to_the_model(
    client: TestClient, engine: Engine, world: World
) -> None:
    accepted(client, world, [create("src/secret_a.py", "SENTINEL_A = 1\n")])
    other_pid, _proposal, _ids = approved_project(client)
    other_plan = build(client, other_pid)
    assert client.post(plans_url(other_pid, f"/{other_plan['id']}/approve")).status_code == 200
    for task in other_plan["tasks"]:
        set_task(engine, task["id"], flags=[], blocking_questions=[])
    provider = serve([create("src/b.py")])
    key = other_plan["tasks"][0]["key"]
    response = client.post(task_url(other_pid, other_plan["id"], key, "/patches"), json={})
    assert response.status_code == 201, response.text
    prompt = provider.requests[-1].prompt
    assert "SENTINEL_A" not in prompt and "secret_a" not in prompt


MODELS = (
    "from enum import Enum\n\n\n"
    "class User:\n    name: str\n    email: str\n\n\n"
    "class Approval:\n    status: str\n\n\n"
    "class AuditLog:\n    entry: str\n\n\n"
    "def helper() -> int:\n    return 1\n"
)
TRIMMED = "class User:\n    name: str\n\n\ndef helper() -> int:\n    return 1\n"


def modify(path: str, content: str) -> dict[str, str]:
    return {"path": path, "change": "modify", "content": content}


def removed_names(body: dict[str, Any]) -> set[tuple[str, str]]:
    return {(r["kind"], r["name"]) for r in body["removed_from_accepted"]}


def test_removed_classes_and_fields_are_reported_and_saving_is_not_blocked(
    client: TestClient, world: World
) -> None:
    accepted(client, world, [create("src/models.py", MODELS)])
    serve([modify("src/models.py", TRIMMED)])
    response = generate(client, world)
    assert response.status_code == 201, response.text
    body = response.json()
    assert removed_names(body) == {
        ("class", "Approval"),
        ("class", "AuditLog"),
        ("field", "User.email"),
        ("field", "Approval.status"),
        ("field", "AuditLog.entry"),
    }
    assert body["needs_justification"] is True
    assert all(
        r["file"] == "src/models.py" and not r["listed"] for r in body["removed_from_accepted"]
    )


def test_listed_removals_are_marked_and_unlisted_are_not(client: TestClient, world: World) -> None:
    accepted(client, world, [create("src/models.py", MODELS)])
    serve(
        [modify("src/models.py", TRIMMED)],
        removals=[{"file": "src/models.py", "name": "AuditLog", "reason": "replaced"}],
    )
    items = generate(client, world).json()["removed_from_accepted"]
    listed = {r["name"]: r["listed"] for r in items}
    assert listed["AuditLog"] is True and listed["Approval"] is False


def test_accept_is_blocked_until_each_removal_is_confirmed(
    client: TestClient, world: World
) -> None:
    pid = world[0]
    accepted(client, world, [create("src/models.py", MODELS)])
    serve(
        [
            modify(
                "src/models.py",
                "class User:\n    name: str\n    email: str\n\n\n"
                "def helper() -> int:\n    return 1\n",
            )
        ]
    )
    patch = generate(client, world).json()
    assert {r["name"] for r in patch["removed_from_accepted"]} == {
        "Approval",
        "AuditLog",
        "Approval.status",
        "AuditLog.entry",
    }
    refused = review(client, pid, patch["id"])
    assert (
        refused.status_code == 409 and refused.json()["detail"]["code"] == "removals_not_confirmed"
    )
    confirmed = [
        {"file": r["file"], "name": r["name"], "reason": "intended"}
        for r in patch["removed_from_accepted"]
    ]
    partial = client.post(
        api(pid, f"/patches/{patch['id']}/accept"),
        json={"reviewer": "ana", "reason": "ok", "confirmed_removals": confirmed[:-1]},
    )
    assert partial.status_code == 409
    done = client.post(
        api(pid, f"/patches/{patch['id']}/accept"),
        json={"reviewer": "ana", "reason": "ok", "confirmed_removals": confirmed},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "accepted"
    assert len(done.json()["coverage"]["removal_confirmations"]) == len(confirmed)


def test_rejecting_a_patch_with_removals_needs_no_confirmation(
    client: TestClient, world: World
) -> None:
    accepted(client, world, [create("src/models.py", MODELS)])
    serve([modify("src/models.py", TRIMMED)])
    patch = generate(client, world).json()
    assert review(client, world[0], patch["id"], "reject").status_code == 200


def test_patch_without_removals_has_no_gate(client: TestClient, world: World) -> None:
    accepted(client, world, [create("src/models.py", MODELS)])
    serve([modify("src/models.py", MODELS + "\n\nclass Extra:\n    pass\n")])
    patch = generate(client, world).json()
    assert patch["removed_from_accepted"] == [] and patch["needs_justification"] is False
    assert review(client, world[0], patch["id"]).status_code == 200


@pytest.mark.parametrize(
    ("path", "old", "new", "expected"),
    [
        ("c.json", '{"a": {"b": 1, "c": 2}, "d": 3}', '{"a": {"b": 1}}', {"a.c", "d"}),
        ("c.yaml", "a:\n  b: 1\n  c: 2\nd: 3\n", "a:\n  b: 1\n", {"a.c", "d"}),
        ("c.toml", "[tool]\nx = 1\ny = 2\n", "[tool]\nx = 1\n", {"tool.y"}),
        ("n.md", "one\ntwo\nthree\n", "one\nthree\n", {"two"}),
    ],
)
def test_non_python_removals_by_keys_or_lines(
    client: TestClient, world: World, path: str, old: str, new: str, expected: set[str]
) -> None:
    accepted(client, world, [create(path, old)])
    serve([modify(path, new)])
    body = generate(client, world).json()
    assert {r["name"] for r in body["removed_from_accepted"]} == expected
    assert {r["kind"] for r in body["removed_from_accepted"]} <= {"key", "line"}


def test_removals_only_compare_with_the_same_projects_accepted_file(
    client: TestClient, engine: Engine, world: World
) -> None:
    accepted(client, world, [create("src/models.py", MODELS)])
    other_pid, _proposal, _ids = approved_project(client)
    other_plan = build(client, other_pid)
    assert client.post(plans_url(other_pid, f"/{other_plan['id']}/approve")).status_code == 200
    for task in other_plan["tasks"]:
        set_task(engine, task["id"], flags=[], blocking_questions=[])
    serve([create("src/models.py", TRIMMED)])
    key = other_plan["tasks"][0]["key"]
    response = client.post(task_url(other_pid, other_plan["id"], key, "/patches"), json={})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["files"][0]["change"] == "create"
    assert body["removed_from_accepted"] == [] and body["needs_justification"] is False


def test_provider_failure_stores_nothing(client: TestClient, engine: Engine, world: World) -> None:
    from app.llm.exceptions import LLMTimeoutError

    use(FakeLLMProvider(error=LLMTimeoutError("boom")))
    response = generate(client, world)
    assert response.status_code >= 500 or response.status_code == 504
    assert patch_count(engine) == 0


def test_projects_are_isolated(client: TestClient, engine: Engine, world: World) -> None:
    first = accepted(client, world, [create("src/a.py")])
    other_pid, _proposal, _ids = approved_project(client)
    assert client.get(api(other_pid, f"/patches/{first['id']}")).status_code == 404
    assert review(client, other_pid, first["id"], "reject").status_code == 404
    assert client.get(api(other_pid, "/workspace")).json() == {"files": []}
    assert client.get(api(world[0], "/workspace")).json()["files"][0]["path"] == "src/a.py"
    other_plan = build(client, other_pid)
    client.post(plans_url(other_pid, f"/{other_plan['id']}/approve"))
    foreign = client.get(
        task_url(world[0], other_plan["id"], other_plan["tasks"][0]["key"], "/patches")
    )
    assert foreign.status_code == 404


# ---- export ----------------------------------------------------------------------------------


def export(client: TestClient, pid: str, **body: Any) -> Response:
    response: Response = client.post(api(pid, "/workspace/export"), json=body)
    return response


def test_export_writes_only_accepted_files_and_a_manifest(
    client: TestClient, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = world[0]
    root = tmp_path / "ws"
    workspace_root(monkeypatch, str(root))
    serve([create("src/rejected.py")])
    rejected = generate(client, world).json()
    review(client, pid, rejected["id"], "reject")
    accepted(client, world, [create("src/a.py", "a = 1\n"), create("docs/readme.md", "# x\n")])
    response = export(client, pid)
    assert response.status_code == 200, response.text
    folder = root / pid
    assert response.json()["folder"] == str(folder.resolve())
    assert (folder / "src" / "a.py").read_text(encoding="utf-8") == "a = 1\n"
    assert not (folder / "src" / "rejected.py").exists()
    assert set(json.loads((folder / MANIFEST_NAME).read_text())["files"]) == {
        "src/a.py",
        "docs/readme.md",
    }
    again = export(client, pid).json()
    assert again["written"] == [] and sorted(again["unchanged"]) == ["docs/readme.md", "src/a.py"]


def test_export_refuses_foreign_files_unless_confirmed(
    client: TestClient, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = world[0]
    root = tmp_path / "ws"
    workspace_root(monkeypatch, str(root))
    accepted(client, world, [create("src/a.py", "a = 1\n")])
    folder = root / pid
    (folder / "src").mkdir(parents=True)
    (folder / "mine.txt").write_text("keep me", encoding="utf-8")
    (folder / "src" / "a.py").write_text("# hand written\n", encoding="utf-8")
    refused = export(client, pid)
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "foreign_files" and detail["total"] == 2
    assert (folder / "src" / "a.py").read_text(encoding="utf-8") == "# hand written\n"
    assert not (folder / MANIFEST_NAME).exists()
    done = export(client, pid, confirm_overwrite=True)
    assert done.status_code == 200 and done.json()["overwritten"] == ["src/a.py"]
    assert (folder / "mine.txt").read_text(encoding="utf-8") == "keep me"
    assert (folder / "src" / "a.py").read_text(encoding="utf-8") == "a = 1\n"


def test_edited_exported_file_counts_as_foreign_and_nothing_is_deleted(
    client: TestClient, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = world[0]
    root = tmp_path / "ws"
    workspace_root(monkeypatch, str(root))
    accepted(client, world, [create("src/a.py"), create("src/b.py")])
    assert export(client, pid).status_code == 200
    folder = root / pid
    (folder / "src" / "b.py").write_text("# edited by hand\n", encoding="utf-8")
    assert export(client, pid).status_code == 409
    (folder / "src" / "b.py").write_bytes(b"x = 1\n")
    accepted(client, world, [{"path": "src/a.py", "change": "delete", "content": ""}])
    result = export(client, pid)
    assert result.status_code == 200
    assert result.json()["left_on_disk"] == ["src/a.py"]
    assert (folder / "src" / "a.py").exists()


def test_export_needs_a_configured_root_outside_the_repository(
    client: TestClient, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = world[0]
    accepted(client, world, [create("src/a.py")])
    workspace_root(monkeypatch, "")
    assert "not configured" in export(client, pid).text
    workspace_root(monkeypatch, "relative/dir")
    assert export(client, pid).status_code == 409
    inside = REPO_ROOT / "generated-workspaces"
    workspace_root(monkeypatch, str(inside))
    response = export(client, pid)
    assert response.status_code == 409 and "outside the ArchPilotAI repository" in response.text
    assert not inside.exists()


def test_export_of_an_empty_workspace_is_refused(
    client: TestClient, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root(monkeypatch, str(tmp_path / "ws"))
    assert export(client, world[0]).status_code == 409


def test_export_refuses_symlinks(
    client: TestClient, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = world[0]
    root = tmp_path / "ws"
    workspace_root(monkeypatch, str(root))
    accepted(client, world, [create("src/a.py")])
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / pid).mkdir(parents=True)
    try:
        os.symlink(outside, root / pid / "src", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    assert export(client, pid, confirm_overwrite=True).status_code == 409
    assert list(outside.iterdir()) == []


# ---- nothing is executed ---------------------------------------------------------------------


def test_generated_code_is_never_executed_or_imported(
    client: TestClient, world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "executed.marker"
    root = tmp_path / "ws"
    workspace_root(monkeypatch, str(root))
    before = set(sys.modules)
    code = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import evil_generated_module\n"
        "__import__('os').system('echo pwned')\n"
    )
    yaml_text = f"x: !!python/object/apply:builtins.open [{str(marker)!r}, w]\n"
    serve(
        [
            create("src/evil.py", code),
            create("src/evil_generated_module.py", code),
            create("cfg/evil.yaml", yaml_text),
        ]
    )
    created = generate(client, world)
    assert created.status_code == 201, created.text
    assert review(client, world[0], created.json()["id"]).status_code == 200
    assert export(client, world[0]).status_code == 200
    assert not marker.exists()
    assert "evil_generated_module" not in sys.modules
    assert not {m for m in set(sys.modules) - before if "evil" in m}


FORBIDDEN_CALLS = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "import_module",
    "run",
    "system",
    "Popen",
}
FORBIDDEN_MODULES = {"subprocess", "importlib", "runpy", "pty"}


@pytest.mark.parametrize(
    "relative",
    [
        "app/domain/patch.py",
        "app/services/patch.py",
        "app/services/workspace_export.py",
        "app/repositories/patch.py",
        "app/llm/patch_prompt.py",
    ],
)
def test_patch_modules_contain_no_execution_primitives(relative: str) -> None:
    source = (REPO_ROOT / "backend" / relative).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            builtin_compile = name == "compile" and isinstance(func, ast.Name)
            if name != "compile" or builtin_compile:
                assert name not in FORBIDDEN_CALLS, f"{relative}: call to {name}"
            if name == "load" and isinstance(func, ast.Attribute):
                raise AssertionError(f"{relative}: unsafe load call")
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & FORBIDDEN_MODULES
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in FORBIDDEN_MODULES


# ---- MCP -------------------------------------------------------------------------------------


def test_get_patch_and_workspace_tree(
    client: TestClient, world: World, mcp_ctx: ToolContext
) -> None:
    pid, _plan, tasks = world
    key = next(iter(tasks))
    with pytest.raises(ToolInputError, match="no patch yet"):
        tools.get_patch(mcp_ctx, pid, key)
    assert tools.get_workspace_tree(mcp_ctx, pid)["files"] == []
    serve([create("src/a.py", "a = 1\n")], notes=["first"])
    first = generate(client, world).json()
    proposed = tools.get_patch(mcp_ctx, pid, key)
    assert (proposed["status"], proposed["version"], proposed["notes"]) == (
        "proposed",
        1,
        ["first"],
    )
    review(client, pid, first["id"])
    serve([create("src/b.py", "b = 1\n")])
    generate(client, world)
    latest_accepted = tools.get_patch(mcp_ctx, pid, key)
    assert latest_accepted["version"] == 1 and latest_accepted["status"] == "accepted"
    assert tools.get_patch(mcp_ctx, pid, key, 2)["files"][0]["path"] == "src/b.py"
    assert [v["status"] for v in latest_accepted["versions"]] == ["accepted", "proposed"]
    with pytest.raises(ToolInputError, match="version 9"):
        tools.get_patch(mcp_ctx, pid, key, 9)
    with pytest.raises(ToolInputError, match="Unknown task_key"):
        tools.get_patch(mcp_ctx, pid, "T-999")
    tree = tools.get_workspace_tree(mcp_ctx, pid)
    assert [f["path"] for f in tree["files"]] == ["src/a.py"] and tree["truncated"] is False


def test_mcp_tools_report_truncation(
    client: TestClient, world: World, mcp_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _plan, tasks = world
    key = next(iter(tasks))
    accepted(client, world, [create("src/a.py", "a = 1\n" * 20), create("src/b.py")])
    monkeypatch.setattr(tools, "MAX_PATCH_FILE_CHARS", 10)
    monkeypatch.setattr(tools, "MAX_PATCH_FILES", 1)
    monkeypatch.setattr(tools, "MAX_TREE_ENTRIES", 1)
    patch = tools.get_patch(mcp_ctx, pid, key)
    assert patch["truncated"] is True
    assert patch["truncated_sections"]["files"] == {"returned": 1, "total": 2}
    assert "files[src/a.py].content" in patch["truncated_sections"]
    assert len(patch["files"][0]["content"]) == 10
    tree = tools.get_workspace_tree(mcp_ctx, pid)
    assert tree["total"] == 2 and len(tree["files"]) == 1 and tree["truncated"] is True
    assert tree["truncated_sections"] == {"files": {"returned": 1, "total": 2}}


def test_mcp_tools_are_project_scoped(
    client: TestClient, world: World, mcp_ctx: ToolContext
) -> None:
    pid, _plan, tasks = world
    accepted(client, world, [create("src/a.py")])
    other, _proposal, _ids = approved_project(client)
    assert tools.get_workspace_tree(mcp_ctx, other)["files"] == []
    with pytest.raises(ToolInputError):
        tools.get_patch(mcp_ctx, other, next(iter(tasks)))


def test_get_plan_reports_derived_task_status(
    client: TestClient, world: World, mcp_ctx: ToolContext
) -> None:
    pid, _plan, tasks = world
    key = next(iter(tasks))
    accepted(client, world, [create("src/a.py")])
    content = tools.get_plan(mcp_ctx, pid, sections=["tasks"])["content"]["tasks"]
    assert {t["key"]: t["status"] for t in content}[key] == "accepted"


# ---- schema ----------------------------------------------------------------------------------


def test_migration_on_isolated_sqlite_file_roundtrips(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'patches.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "code_patches",
            "patch_files",
            "workspace_files",
            "technology_confirmations",
        } <= tables
        columns = {c["name"] for c in inspect(engine).get_columns("code_patches")}
        assert {"project_id", "plan_id", "task_id", "version", "status", "review_reason"} <= columns
    finally:
        engine.dispose()
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")


def test_models_declare_composite_foreign_keys() -> None:
    for model, target in ((CodePatch, "plan_tasks"), (PatchFile, "code_patches")):
        table: Any = model.__table__
        pairs = [
            {c.parent.name for c in fk.elements}
            for fk in table.foreign_key_constraints
            if fk.referred_table.name == target
        ]
        assert {"project_id"} <= pairs[0], model


def test_removal_contract_key_sets_are_locked() -> None:
    assert set(RemovalItemOut.model_fields) == {"kind", "name", "file", "listed", "reason"}
    assert set(RemovalMissingOut.model_fields) == {"file", "name"}
    assert set(RemovalsNotConfirmedBody.model_fields) == {"code", "message", "missing"}
    assert {"removed_from_accepted", "needs_justification"} <= set(PatchResponse.model_fields)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_frontend_fixtures_match_the_models(name: str) -> None:
    expected = render(FIXTURES[name]())
    assert (FIXTURE_DIR / name).read_text(encoding="utf-8") == expected, (
        "regenerate with: python -m tests.contract_fixtures"
    )


def test_real_responses_have_the_contract_keys(client: TestClient, world: World) -> None:
    accepted(client, world, [create("src/models.py", MODELS)])
    serve(
        [modify("src/models.py", TRIMMED)],
        removals=[{"file": "src/models.py", "name": "AuditLog", "reason": "replaced"}],
    )
    patch = generate(client, world).json()
    assert patch["removed_from_accepted"]
    for item in patch["removed_from_accepted"]:
        assert set(item) == set(RemovalItemOut.model_fields)
    refused = review(client, world[0], patch["id"])
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert set(detail) == set(RemovalsNotConfirmedBody.model_fields)
    assert detail["code"] == "removals_not_confirmed"
    assert all(set(m) == set(RemovalMissingOut.model_fields) for m in detail["missing"])
