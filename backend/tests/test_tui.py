"""The retro TUI: themes, keyboard navigation, approval confirmations, live runs, no secrets."""

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from textual.pilot import Pilot
from textual.widgets import DataTable, Input, ListView

from app.core.db import make_session_factory
from app.llm.contracts import LLMRequest, LLMResponse
from app.llm.fake_provider import FakeLLMProvider
from app.models.git_remote import GitRemoteSettings
from app.services import agent as agent_service
from app.tools import get_registry
from app.tui.app import ArchPilotApp
from app.tui.backend import Backend, Target, TuiError, _guard, pat_label
from app.tui.screens import (
    ChecklistScreen,
    CIScreen,
    ConfirmScreen,
    HelpScreen,
    MissionScreen,
    PaletteScreen,
    PatchScreen,
    RunScreen,
    TasksScreen,
)
from app.tui.theme import (
    DEFAULT_THEME,
    THEMES,
    UnknownThemeError,
    badge,
    bar,
    resolve_theme,
)
from app.tui.widgets import Cursor, Plain
from tests.test_checklists import approved_two_steps
from tests.test_patches import World, create, generate, serve
from tests.test_patches import world as _world_fixture

world = _world_fixture  # re-exported so pytest finds the fixture here

Script = Callable[[Pilot[None]], Awaitable[None]]
REMOTE = "https://github.com/acme/demo.git"


@pytest.fixture(autouse=True)
def clean_agent_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("NO_COLOR", raising=False)
    yield
    agent_service._active.clear()
    if agent_service._agent_lock.locked():
        agent_service._agent_lock.release()


def make_app(engine: Engine, provider: Any = None, **kwargs: Any) -> ArchPilotApp:
    chosen = provider or FakeLLMProvider()
    backend = Backend(make_session_factory(engine), provider=lambda: chosen)
    return ArchPilotApp(backend, **kwargs)


def drive(app: ArchPilotApp, script: Script) -> None:
    async def main() -> None:
        async with app.run_test(size=(180, 60)) as pilot:
            await pilot.pause()
            await script(pilot)

    asyncio.run(main())


async def until(pilot: Pilot[None], condition: Callable[[], bool], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the TUI"
        await pilot.pause(0.05)


def text(app: ArchPilotApp, selector: str) -> str:
    return app.screen.query_one(selector, Plain).text


async def type_text(pilot: Pilot[None], value: str) -> None:
    for ch in value:
        await pilot.press("space" if ch == " " else ch)


def add_remote(engine: Engine, pid: str) -> None:
    with Session(engine) as session:
        session.add(
            GitRemoteSettings(
                project_id=uuid.UUID(pid),
                provider="github",
                remote_url=REMOTE,
                clone_folder="demo",
                base_branch="main",
                branch_prefix="agent/",
                author_name="Ana",
                author_email="ana@example.com",
                auth_method="pat",
                pat_ref="archpilot/pat/ref-do-not-show",
                pat_provider="github",
                pat_last4="abcd",
            )
        )
        session.commit()


def proposed_patch(client: TestClient, world: World) -> str:
    serve([create("src/app.py", "def run() -> int:\n    return 1\n")])
    created = generate(client, world)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


# ---- themes, NO_COLOR, look --------------------------------------------------------------------


def test_theme_selection_and_no_color() -> None:
    assert set(THEMES) == {"cyber-8", "green-phosphor", "amber-terminal", "mono"}
    assert resolve_theme(None, {}) == DEFAULT_THEME == "cyber-8"
    assert resolve_theme("amber-terminal", {}) == "amber-terminal"
    assert resolve_theme("amber-terminal", {"NO_COLOR": "1"}) == "mono"
    assert resolve_theme(None, {"NO_COLOR": ""}) == "cyber-8"  # empty means unset
    with pytest.raises(UnknownThemeError):
        resolve_theme("rainbow", {})


def test_bars_badges_and_pat_label() -> None:
    assert bar(5, 10, width=10) == "[█████░░░░░]  50%"
    assert bar(50, 10, width=4) == "[████] 100%"
    assert bar(1, 0, width=3) == "[░░░] no limit"
    assert badge("accepted") == "[  OK  ]" and badge("weird") == "[WEIRD ]"
    assert pat_label("abcd") == "ends with ...abcd" and pat_label(None) == "none"


def test_app_applies_theme_animation_and_palette_theme_switch(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(engine, theme="green-phosphor", animation=False)

    async def script(pilot: Pilot[None]) -> None:
        assert app.theme == "green-phosphor"
        assert not app.screen.query_one(Cursor).blinking
        await pilot.press("colon")
        assert isinstance(app.screen, PaletteScreen)
        await type_text(pilot, "theme amber-terminal")
        await pilot.press("enter")
        assert app.theme == "amber-terminal"
        monkeypatch.setenv("NO_COLOR", "1")
        await pilot.press("colon")
        await type_text(pilot, "theme cyber-8")
        await pilot.press("enter")
        assert app.theme == "amber-terminal"  # NO_COLOR: no colour theme can be chosen

    drive(app, script)


def test_cursor_blinks_with_animation(engine: Engine) -> None:
    app = make_app(engine)

    async def script(pilot: Pilot[None]) -> None:
        cursor = app.screen.query_one(Cursor)
        assert cursor.blinking
        first = cursor.lit
        await until(pilot, lambda: cursor.lit != first, timeout=3)

    drive(app, script)


# ---- keyboard navigation -----------------------------------------------------------------------


def test_keyboard_navigation_through_the_screens(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid, plan, tasks = world
    add_remote(engine, pid)
    app = make_app(engine, animation=False)
    first_key = next(iter(tasks))

    async def script(pilot: Pilot[None]) -> None:
        assert isinstance(app.screen, MissionScreen)
        await pilot.press("enter")  # the only project
        mission = text(app, "#mission")
        assert "Plan:         v1 approved" in mission
        assert f"Remote:       {REMOTE}" in mission
        assert "PAT:          ends with ...abcd" in mission
        assert "ref-do-not-show" not in mission
        assert app.plan_id == uuid.UUID(plan["id"])

        await pilot.press("t")
        assert isinstance(app.screen, TasksScreen)
        await pilot.press("enter")
        assert isinstance(app.screen, ChecklistScreen)
        assert app.screen.task_key == first_key
        assert f"Task {first_key}" in text(app, "#checklist-head")

        await pilot.press("escape")
        assert isinstance(app.screen, TasksScreen)
        await pilot.press("down", "enter")
        assert isinstance(app.screen, ChecklistScreen)
        assert app.screen.task_key == list(tasks)[1]

        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
        assert "Approval-tier actions" in text(app, "#help-text")
        await pilot.press("escape")
        assert isinstance(app.screen, ChecklistScreen)

        await pilot.press("colon")
        await type_text(pilot, "mission")
        await pilot.press("enter")
        assert isinstance(app.screen, MissionScreen)
        await pilot.press("escape")  # nothing below mission control
        assert isinstance(app.screen, MissionScreen)

    drive(app, script)


def test_start_options_open_project_and_task(
    client: TestClient, engine: Engine, world: World
) -> None:
    _pid, _plan, tasks = world
    key = list(tasks)[1]
    app = make_app(engine, project="ANY", task=key, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        await pilot.pause()
        assert isinstance(app.screen, MissionScreen)  # no project named ANY

    drive(app, script)

    name = make_app(engine).backend.projects()[0].name
    app = make_app(engine, project=name.upper(), task=key.lower(), animation=False)

    async def opened(pilot: Pilot[None]) -> None:
        await until(pilot, lambda: isinstance(app.screen, ChecklistScreen))
        assert app.screen.task_key == key  # type: ignore[attr-defined]

    drive(app, opened)


def test_start_change_option_records_and_analyses_a_change_request(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    name = make_app(engine).backend.projects()[0].name
    app = make_app(engine, project=name, change="Fix the typo in the list label", animation=False)

    async def script(pilot: Pilot[None]) -> None:
        await pilot.pause()
        await until(pilot, lambda: bool(client.get(f"/api/projects/{pid}/change-requests").json()))

    drive(app, script)
    created = client.get(f"/api/projects/{pid}/change-requests").json()
    assert [c["key"] for c in created] == ["CR-001"]
    assert created[0]["status"] == "analysed" and created[0]["impact_class"] == "code_only"


# ---- approval confirmation ---------------------------------------------------------------------


def test_accept_needs_the_blocking_confirmation_with_name_and_reason(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    add_remote(engine, pid)
    patch_id = proposed_patch(client, world)
    app = make_app(engine, animation=False)

    def status() -> str:
        return app.backend.patch(uuid.UUID(pid), uuid.UUID(patch_id)).status

    async def script(pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await pilot.press("colon")
        await type_text(pilot, f"patch {patch_id}")
        await pilot.press("enter")
        assert isinstance(app.screen, PatchScreen)
        assert "+def run() -> int:" in text(app, "#diff")
        assert "src/app.py" in text(app, "#diff")
        assert "Coverage:" in text(app, "#patch-head")

        await pilot.press("a")
        assert isinstance(app.screen, ConfirmScreen)
        shown = text(app, "#confirm-target")
        assert "Action:     ACCEPT patch v1" in shown
        assert "Repository: demo" in shown
        assert f"Remote:     {REMOTE}" in shown
        assert "Branch:     agent/" in shown
        await pilot.press("a", "x", "u")  # typed into the name box, not patch actions
        await pilot.press("escape")
        assert isinstance(app.screen, PatchScreen) and status() == "proposed"

        await pilot.press("a")
        await pilot.press("tab", "tab", "enter")  # Confirm with no name or reason
        assert isinstance(app.screen, ConfirmScreen)
        assert "Type your name and a reason" in text(app, "#confirm-error")
        assert status() == "proposed"

        app.screen.query_one("#confirm-name", Input).focus()
        await type_text(pilot, "Ana")
        await pilot.press("enter")
        await type_text(pilot, "Reviewed")
        await pilot.press("enter", "enter")  # focus Confirm, then press it
        await until(pilot, lambda: isinstance(app.screen, PatchScreen))
        assert status() == "accepted"
        assert "Reviewed by Ana: Reviewed" in text(app, "#patch-head")

    drive(app, script)


def test_push_and_pull_request_show_exact_target_and_cancel_does_nothing(
    client: TestClient, engine: Engine, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = world[0]
    add_remote(engine, pid)
    patch_id = proposed_patch(client, world)
    calls: list[tuple[str, Any]] = []

    def fake_push(self: Backend, p: Any, b: str, n: str) -> str:
        calls.append(("push", (b, n)))
        return b

    def fake_pr(self: Backend, p: Any, i: Any) -> str:
        calls.append(("pr", i))
        return "created"

    monkeypatch.setattr(Backend, "push", fake_push)
    monkeypatch.setattr(Backend, "pull_request", fake_pr)
    app = make_app(engine, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        app.open_patch(uuid.UUID(patch_id))
        await pilot.pause()
        await pilot.press("u")
        assert isinstance(app.screen, ConfirmScreen)
        shown = text(app, "#confirm-target")
        assert "PUSH branch agent/" in shown and f"Remote:     {REMOTE}" in shown
        assert "Repository: demo" in shown and "abcd" not in shown
        await pilot.press("escape")
        assert calls == []

        await pilot.press("u")
        await type_text(pilot, "Ana")
        await pilot.press("enter", "enter")
        await until(pilot, lambda: bool(calls))
        branch = calls[0][1][0]
        assert branch.startswith("agent/") and calls[0][1][1] == "Ana"

        await pilot.press("o")
        assert isinstance(app.screen, ConfirmScreen)
        assert f"OPEN PULL REQUEST {branch} -> main" in text(app, "#confirm-target")
        await pilot.press("escape")
        assert len(calls) == 1

    drive(app, script)


def test_ci_branch_approval_needs_confirmation(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved: list[str] = []
    target = Target("APPROVE CI BRANCH for run x", "demo", REMOTE, "agent/t1-ci")
    monkeypatch.setattr(
        Backend,
        "ci",
        lambda self, p, r: {
            "branch": "agent/t1-ci",
            "approval": "pending",
            "phase": "waiting_approval",
            "iteration": 1,
            "max_iterations": 3,
            "workflow_url": "https://github.com/acme/demo/actions/runs/7",
            "workflow_status": "failure",
            "counts": {"new": 2, "fixed": 1, "pre_existing": 4},
        },
    )
    monkeypatch.setattr(Backend, "ci_target", lambda self, p, r: target)

    def fake_approve(self: Backend, p: Any, r: Any, n: str) -> str:
        approved.append(n)
        return "approved"

    monkeypatch.setattr(Backend, "ci_approve", fake_approve)
    app = make_app(engine, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        app.push_screen(CIScreen(uuid.uuid4(), uuid.uuid4()))
        await pilot.pause()
        shown = text(app, "#ci")
        assert "Iterations:  [" in shown and "1/3" in shown
        assert "https://github.com/acme/demo/actions/runs/7" in shown
        assert "New: 2   Fixed: 1   Pre-existing: 4" in shown
        await pilot.press("a")
        assert isinstance(app.screen, ConfirmScreen)
        assert "Branch:     agent/t1-ci" in text(app, "#confirm-target")
        await pilot.press("escape")
        assert approved == []
        await pilot.press("a")
        await type_text(pilot, "Ana")
        await pilot.press("enter", "enter")
        await until(pilot, lambda: approved == ["Ana"])

    drive(app, script)


def test_the_model_is_never_offered_push_or_pull_request() -> None:
    exposed = {t.name for t in get_registry().exposed("agent")}
    assert exposed
    assert not {n for n in exposed if "push" in n or "pull_request" in n or "commit" in n}


def test_unexpected_errors_show_the_type_only() -> None:
    with pytest.raises(TuiError) as caught, _guard():
        raise RuntimeError("postgresql://user:hunter2@db/x")
    assert str(caught.value) == "Unexpected error (RuntimeError)"


# ---- live run ----------------------------------------------------------------------------------


class GatedProvider(FakeLLMProvider):
    """Each model call waits for the test to open the gate: the run advances step by step."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(agent_script=replies)
        self.gate = threading.Semaphore(0)

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert self.gate.acquire(timeout=30)
        return super().generate(request)


def action(name: str, /, **args: Any) -> str:
    return json.dumps({"action": name, "args": args})


def test_live_run_updates_stop_and_resume(client: TestClient, engine: Engine, world: World) -> None:
    provider = GatedProvider(
        [
            action("write_file", path="src/a.py", content="x = 1\n"),
            action("list_files"),
            action("finish", summary="done"),
        ]
    )
    app = make_app(engine, provider, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        await pilot.press("enter", "t", "g")  # project, tasks, run the first task
        await until(pilot, lambda: isinstance(app.screen, RunScreen))
        screen = app.screen
        assert isinstance(screen, RunScreen)
        await until(pilot, lambda: screen.view is not None)
        assert "running" in text(app, "#run-head")
        assert "Waiting for the first step" in text(app, "#run-steps")
        budget = text(app, "#run-budget")
        assert budget.startswith("Tokens [") and "Time   [" in budget and "Steps  [" in budget

        provider.gate.release()  # one step
        await until(pilot, lambda: "write_file" in text(app, "#run-steps"))
        steps = text(app, "#run-steps")
        assert "tool=write_file" in steps and "tier=draft" in steps and "src/a.py" in steps
        updates = screen.updates
        await until(pilot, lambda: screen.updates > updates)  # still polling

        await pilot.press("s")
        assert "Stop requested" in text(app, "#status")
        provider.gate.release(5)
        await until(pilot, lambda: screen.view is not None and screen.view.status != "running")
        assert screen.view is not None and screen.view.status == "stopped"
        assert "[ STOP ]" in text(app, "#run-head")
        assert screen._timer is None  # polling ends with the run

        await pilot.press("r")  # a task-level run has no checkpoint to resume
        assert "Only checklist step runs can be resumed" in text(app, "#status")

    drive(app, script)
    provider.gate.release(10)
    deadline = time.monotonic() + 10
    while agent_service._active and time.monotonic() < deadline:
        time.sleep(0.05)


# ---- arrow-key navigation ----------------------------------------------------------------------


def table(app: ArchPilotApp, selector: str) -> DataTable[Any]:
    return app.screen.query_one(selector, DataTable)


def test_arrows_select_rows_and_enter_activates_on_mission_and_tasks(
    client: TestClient, engine: Engine, world: World
) -> None:
    _pid, _plan, tasks = world
    app = make_app(engine, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        assert app.screen.focused is table(app, "#projects")
        assert table(app, "#projects").cursor_type == "row"
        await pilot.press("down", "up", "enter")
        assert "Plan:" in text(app, "#mission")
        await pilot.press("tab")  # to the detail pane
        assert app.screen.focused is app.screen.query_one("#mission")
        await pilot.press("left")
        assert app.screen.focused is table(app, "#projects")
        await pilot.press("right")
        assert app.screen.focused is app.screen.query_one("#mission")
        await pilot.press("shift+tab")
        assert app.screen.focused is table(app, "#projects")

        await pilot.press("t")  # letter shortcut still works
        assert isinstance(app.screen, TasksScreen)
        assert table(app, "#tasks").cursor_type == "row"
        await pilot.press("down", "down")
        assert table(app, "#tasks").cursor_row == min(2, len(tasks) - 1)
        await pilot.press("up")
        row = table(app, "#tasks").cursor_row
        await pilot.press("enter")
        assert isinstance(app.screen, ChecklistScreen)
        assert app.screen.task_key == list(tasks)[row]

    drive(app, script)


def test_checklist_panes_tab_arrows_and_letter_fallbacks(
    client: TestClient, engine: Engine, world: World
) -> None:
    approved_two_steps(client, engine, world)
    app = make_app(engine, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        await pilot.press("enter", "t", "enter")
        assert isinstance(app.screen, ChecklistScreen)
        steps, runs = table(app, "#steps"), table(app, "#runs")
        assert steps.cursor_type == runs.cursor_type == "row"
        assert app.screen.focused is steps
        await pilot.press("tab")
        assert app.screen.focused is runs
        await pilot.press("shift+tab")
        assert app.screen.focused is steps
        await pilot.press("right")
        assert app.screen.focused is runs
        await pilot.press("left")
        assert app.screen.focused is steps

        assert table(app, "#steps").row_count == 2
        await pilot.press("down", "enter")  # an unrun step: no navigation, a hint
        assert isinstance(app.screen, ChecklistScreen)
        assert table(app, "#steps").cursor_row == 1
        assert "has not run yet" in text(app, "#status")
        await pilot.press("p")  # letter shortcut alongside arrows
        assert text(app, "#status") == "The selected step has no patch."

    drive(app, script)


def test_patch_file_list_arrows_enter_and_letter_shortcuts(
    client: TestClient, engine: Engine, world: World
) -> None:
    pid = world[0]
    serve(
        [
            create("src/a.py", "def a() -> int:\n    return 1\n"),
            create("src/b.py", "def b() -> int:\n    return 2\n"),
        ]
    )
    created = generate(client, world)
    assert created.status_code == 201, created.text
    patch_id = str(created.json()["id"])
    app = make_app(engine, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        app.open_patch(uuid.UUID(patch_id))
        await pilot.pause()
        assert isinstance(app.screen, PatchScreen)
        files = table(app, "#files")
        assert files.cursor_type == "row" and app.screen.focused is files
        assert files.row_count == 2
        first = text(app, "#diff")
        await pilot.press("down")
        await pilot.pause()
        assert files.cursor_row == 1 and text(app, "#diff") != first
        await pilot.press("up")
        await pilot.pause()
        assert text(app, "#diff") == first

        await pilot.press("enter")  # activate: focus moves to the diff to scroll it
        assert app.screen.focused is app.screen.query_one("#diff-scroll")
        await pilot.press("left")
        assert app.screen.focused is files
        await pilot.press("tab")
        assert app.screen.focused is app.screen.query_one("#diff-scroll")

        await pilot.press("colon")  # palette from a focused pane; arrows stay in its input
        assert isinstance(app.screen, PaletteScreen)
        await type_text(pilot, "abc")
        await pilot.press("left", "left", "right")
        assert app.screen.query_one(Input).value == "abc"
        await pilot.press("escape")
        assert isinstance(app.screen, PatchScreen)

        await pilot.press("a")  # letter shortcut still opens the confirmation
        assert isinstance(app.screen, ConfirmScreen)
        assert pid

    drive(app, script)


def test_palette_opens_from_every_navigable_screen(
    client: TestClient, engine: Engine, world: World
) -> None:
    app = make_app(engine, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        await pilot.press("enter", "t")
        for expected in (TasksScreen, ChecklistScreen):
            assert isinstance(app.screen, expected)
            await pilot.press("colon")
            assert isinstance(app.screen, PaletteScreen)
            await pilot.press("escape")
            assert isinstance(app.screen, expected)
            await pilot.press("down", "enter")

    drive(app, script)


def test_enter_on_the_run_screen_opens_its_patch_like_p(
    client: TestClient, engine: Engine, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeLLMProvider(agent_script=[action("finish", summary="done")])
    app = make_app(engine, provider, animation=False)
    patch_id = uuid.UUID(proposed_patch(client, world))
    original = Backend.run

    def run_with_patch(self: Backend, project_id: uuid.UUID, run_id: uuid.UUID) -> Any:
        return original(self, project_id, run_id).model_copy(update={"patch_id": patch_id})

    async def script(pilot: Pilot[None]) -> None:
        await pilot.press("enter", "t", "g")
        await until(pilot, lambda: isinstance(app.screen, RunScreen))
        run = app.screen
        assert isinstance(run, RunScreen)
        await until(pilot, lambda: run.view is not None and run.view.status != "running")

        await pilot.press("enter")  # the run has no patch yet
        assert isinstance(app.screen, RunScreen)
        assert text(app, "#status") == "This run has no patch yet."

        monkeypatch.setattr(Backend, "run", run_with_patch)
        run.refresh_run()
        await pilot.press("enter")
        assert isinstance(app.screen, PatchScreen) and app.screen.patch_id == patch_id
        await pilot.press("escape")
        assert isinstance(app.screen, RunScreen)
        await pilot.press("p")  # the letter shortcut lands on the same screen
        assert isinstance(app.screen, PatchScreen) and app.screen.patch_id == patch_id

    drive(app, script)


def test_ci_screen_has_no_rows_so_arrows_are_inert_and_the_letter_action_stays(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Backend, "ci", lambda self, p, r: {"branch": "agent/t1-ci"})
    app = make_app(engine, animation=False)

    async def script(pilot: Pilot[None]) -> None:
        app.push_screen(CIScreen(uuid.uuid4(), uuid.uuid4()))
        await pilot.pause()
        assert not app.screen.query(DataTable) and not app.screen.query(ListView)
        before = text(app, "#ci")
        await pilot.press("down", "up", "enter", "left", "right", "tab")
        assert isinstance(app.screen, CIScreen) and text(app, "#ci") == before
        await pilot.press("colon")  # the palette still opens
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, CIScreen)

    drive(app, script)
