"""Reviewer feedback, acceptance-criteria coverage and Django migration rules."""

import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.domain.coverage import CoverageItem, check_coverage, extract_terms, missing_lines
from app.domain.feedback import (
    FEEDBACK_LABEL,
    MAX_FEEDBACK_FILES,
    MAX_FEEDBACK_NOTES,
    MAX_REASON_CHARS,
    build_feedback,
    codes_in,
)
from app.domain.frameworks import FrameworkRules, is_migration_path
from tests.test_agent import clean_agent_state as _clean_agent_state
from tests.test_agent import (
    context_of,
    finish,
    play,
    run_of,
    scripted,
    start,
    write,
)
from tests.test_agent import world as _agent_world
from tests.test_decisions import decide
from tests.test_patches import (
    World,
    api,
    create,
    generate,
    serve,
    set_task,
    task_url,
)
from tests.test_patches import world as _world

world = _world
agent_world = _agent_world
clean_agent_state = _clean_agent_state  # autouse: runs the agent inline

CRITERION = "An admin can set an Invoice to approved"
GOOD = "class Invoice:\n    status = 'approved'\n\n\nROLE = 'admin'\n"


def reject(client: TestClient, pid: str, patch_id: str, reason: str) -> None:
    response = client.post(
        api(pid, f"/patches/{patch_id}/reject"), json={"reviewer": "ana", "reason": reason}
    )
    assert response.status_code == 200, response.text


def first_task(world: World) -> dict[str, Any]:
    return next(iter(world[2].values()))


# ---- feedback ---------------------------------------------------------------------------------


def test_feedback_after_a_rejection_reaches_the_next_generation(
    client: TestClient, world: World
) -> None:
    pid = world[0]
    serve([create("src/a.py")], notes=["first note"])
    first = generate(client, world).json()
    assert first["based_on_version"] is None
    reject(client, pid, first["id"], "Wrong layering")
    provider = serve([create("src/b.py")])
    second = generate(client, world).json()
    block = context_of(provider.requests[-1].prompt)[FEEDBACK_LABEL]
    assert block["from_version"] == 1 and block["reason"] == "Wrong layering"
    assert block["rejected_files"] == [{"path": "src/a.py", "change": "create"}]
    assert block["notes"] == ["first note"]
    assert second["based_on_version"] == 1
    listed = listed = client.get(
        task_url(pid, world[1]["id"], first_task(world)["key"], "/patches")
    ).json()
    assert {p["version"]: p["based_on_version"] for p in listed} == {1: None, 2: 1}


def test_no_feedback_without_a_rejection(client: TestClient, world: World) -> None:
    serve([create("src/a.py")])
    generate(client, world)
    provider = serve([create("src/b.py")])
    generate(client, world)  # the previous patch is proposed, not rejected
    assert FEEDBACK_LABEL not in provider.requests[-1].prompt


def test_feedback_is_bounded() -> None:
    files = [(f"src/f{i}.py", "create") for i in range(MAX_FEEDBACK_FILES + 5)]
    notes = [f"n{i}" for i in range(MAX_FEEDBACK_NOTES + 5)]
    block = build_feedback(version=2, reason="x" * 5000, files=files, notes=notes, requirements={})[
        FEEDBACK_LABEL
    ]
    assert len(block["rejected_files"]) == MAX_FEEDBACK_FILES
    assert block["rejected_files_total"] == len(files)
    assert len(block["notes"]) == MAX_FEEDBACK_NOTES
    assert len(block["reason"]) <= MAX_REASON_CHARS + 1


def test_requirement_codes_in_the_reason_are_looked_up(client: TestClient, world: World) -> None:
    assert codes_in("Violates FR-004 and FR-004, see NFR-012") == ["FR-004", "NFR-012"]
    block = build_feedback(
        version=1,
        reason="Ignores FR-004",
        files=[],
        notes=[],
        requirements={"FR-004": ("Export", "Users export reports"), "FR-009": ("X", "Y")},
    )[FEEDBACK_LABEL]
    assert block["requirements_mentioned"] == [
        {"code": "FR-004", "title": "Export", "description": "Users export reports"}
    ]


def test_feedback_is_per_task_and_project(client: TestClient, world: World) -> None:
    pid, _plan, tasks = world
    keys = list(tasks)
    serve([create("src/a.py")])
    first = generate(client, world, keys[0]).json()
    reject(client, pid, first["id"], "nope")
    provider = serve([create("src/b.py")])
    if len(keys) > 1:
        generate(client, world, keys[1])
        assert FEEDBACK_LABEL not in provider.requests[-1].prompt


def test_the_agent_receives_feedback_and_marks_the_patch(client: TestClient, world: World) -> None:
    pid = world[0]
    serve([create("src/a.py")])
    first = generate(client, world).json()
    reject(client, pid, first["id"], "Too big")
    run, provider = play(client, world, [write("src/g.py"), finish()])
    assert context_of(provider.requests[0].prompt)[FEEDBACK_LABEL]["reason"] == "Too big"
    patch = client.get(api(pid, f"/patches/{run['patch_id']}")).json()
    assert patch["based_on_version"] == 1


# ---- coverage ---------------------------------------------------------------------------------


def test_term_extraction_is_simple_and_documented() -> None:
    assert extract_terms(CRITERION) == ["admin", "approved", "Invoice"]
    assert extract_terms("The Invoice must be saved") == ["Invoice"]


def test_coverage_reports_covered_and_missing_per_criterion() -> None:
    items = [
        CoverageItem("criterion", "AC1", CRITERION),
        CoverageItem("criterion", "AC2", "A Customer can log in"),
    ]
    report = check_coverage(items, {"a.py": GOOD})
    by_ref = {i["ref"]: i for i in report["items"]}
    assert by_ref["AC1"]["status"] == "covered"
    assert by_ref["AC2"]["status"] == "missing" and by_ref["AC2"]["missing"] == ["customer"]
    assert (report["covered"], report["missing"]) == (1, 1)
    assert any("customer" in line for line in missing_lines(report))


def test_patch_stores_coverage(client: TestClient, engine: Engine, world: World) -> None:
    set_task(engine, first_task(world)["id"], acceptance_criteria=[CRITERION])
    serve([create("src/a.py", "class Invoice:\n    pass\n")])
    patch = generate(client, world).json()
    item = patch["coverage"]["items"][0]
    assert item["status"] == "missing" and "approved" in item["missing"]
    serve([create("src/b.py", GOOD)])
    patch = generate(client, world).json()
    assert patch["coverage"]["items"][0]["status"] == "covered"


def test_finish_is_refused_while_items_are_missing(
    client: TestClient, engine: Engine, world: World
) -> None:
    set_task(engine, first_task(world)["id"], acceptance_criteria=[CRITERION])
    run, provider = play(
        client,
        world,
        [write("src/a.py", "class Invoice:\n    pass\n"), finish(), finish()],
        max_steps=6,
    )
    assert "acceptance_missing" in [s["error_code"] for s in run["steps"]]
    assert run["status"] != "finished" or run["patch_id"] is None
    scripted(
        [
            write("src/a.py", "class Invoice:\n    pass\n"),
            finish(),
            write("src/b.py", GOOD),
            finish(),
        ]
    )
    done = run_of(client, world, start(client, world, max_steps=6))
    assert done["patch_id"] and "acceptance_missing" in [s["error_code"] for s in done["steps"]]
    assert provider.requests  # the refusal was sent back to the model


def test_step_limit_lets_finish_through_and_lists_missing_in_the_notes(
    client: TestClient, engine: Engine, world: World
) -> None:
    set_task(engine, first_task(world)["id"], acceptance_criteria=[CRITERION])
    run, _ = play(
        client, world, [write("src/a.py", "class Invoice:\n    pass\n"), finish()], max_steps=2
    )
    assert run["patch_id"]
    patch = client.get(api(world[0], f"/patches/{run['patch_id']}")).json()
    assert any("Not covered at the step limit" in n and "approved" in n for n in patch["notes"])


# ---- Django -----------------------------------------------------------------------------------


def test_migration_paths() -> None:
    assert is_migration_path("shop/migrations/0002_auto.py")
    assert not is_migration_path("shop/migrations/__init__.py")
    assert not is_migration_path("shop/models.py")


def test_django_rules_note_and_app_label() -> None:
    rules = FrameworkRules.detect(["Django"], ["app label: shop"], [])
    assert rules.django and rules.refuses("shop/migrations/0001_initial.py")
    assert "makemigrations shop" in rules.note()
    unknown = FrameworkRules.detect(["Django"], [], [])
    assert "<app_label>" in unknown.note()
    assert not FrameworkRules.detect(["FastAPI"], [], []).refuses("a/migrations/0001_x.py")


def test_single_shot_strips_django_migrations(client: TestClient, world: World) -> None:
    decide(client, world[0], choice="Django")
    serve(
        [create("shop/models.py", "class A:\n    pass\n"), create("shop/migrations/0002_a.py")],
        notes=["n"],
    )
    patch = generate(client, world).json()
    assert [f["path"] for f in patch["files"]] == ["shop/models.py"]
    assert any("makemigrations" in n for n in patch["notes"])


def test_agent_cannot_write_django_migrations(client: TestClient, world: World) -> None:
    decide(client, world[0], choice="Django")
    run, provider = play(
        client, world, [write("shop/migrations/0002_a.py"), write("shop/m.py"), finish()]
    )
    print("RUN", run)
    assert run["steps"][0]["error_code"] == "django_migration"
    assert json.dumps(context_of(provider.requests[0].prompt)).count("makemigrations") >= 1


# ---- header count -----------------------------------------------------------------------------


def test_list_reports_file_counts(client: TestClient, world: World) -> None:
    serve([create("src/a.py"), create("src/b.py")])
    generate(client, world)
    listed = client.get(
        task_url(world[0], world[1]["id"], first_task(world)["key"], "/patches")
    ).json()
    assert [p["file_count"] for p in listed] == [2]
