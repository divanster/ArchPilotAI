"""The TUI screens. Every action is one call to the Backend facade (existing services)."""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Button, DataTable, Footer, Input, Label

from app.api.agent import RunResponse
from app.tui.backend import (
    Backend,
    ChecklistInfo,
    PatchView,
    ProjectRow,
    Target,
    TuiError,
)
from app.tui.theme import badge, bar
from app.tui.widgets import Cursor, Logo, Plain, frame

if TYPE_CHECKING:
    from app.tui.app import ArchPilotApp

W = TypeVar("W", bound=Widget)

_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s@]+@")

POLL_SECONDS = 0.5


def safe_url(url: str) -> str:
    """A remote URL as shown on screen: user info (never expected, never shown) removed."""
    return _URL_CREDENTIALS.sub(r"\1", url)


def _framed(widget: W, label: str) -> W:
    frame(widget, label)  # type: ignore[arg-type]
    return widget


# ---- base --------------------------------------------------------------------------------------


class Base(Screen[None]):
    """Header logo, a title frame, a plain status line with the cursor, and the key footer."""

    TITLE_LABEL = "ARCHPILOT"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back"),
    ]

    @property
    def archpilot(self) -> "ArchPilotApp":
        return self.app  # type: ignore[return-value]

    @property
    def backend(self) -> Backend:
        return self.archpilot.backend

    def compose(self) -> ComposeResult:
        yield Logo()
        yield Label(f"▓▓ {self.TITLE_LABEL} ▓▓", id="title")
        yield from self.body()
        with Horizontal(id="statusbar"):
            yield Plain("READY", id="status")
            yield Cursor(self.archpilot.animation)
        yield Footer()

    def body(self) -> ComposeResult:
        yield from ()

    def say(self, message: str) -> None:
        self.query_one("#status", Plain).set(message)

    def fail(self, message: str) -> None:
        self.say(f"ERROR: {message}")
        self.app.notify(message, title="Error", severity="error", markup=False)

    def attempt(self, call: Callable[[], Any]) -> Any:
        """Run one backend call; a refusal is shown in plain text and returns None."""
        try:
            return call()
        except TuiError as exc:
            self.fail(str(exc))
            return None

    def action_back(self) -> None:
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()


# ---- confirmation (approval tier) --------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    name: str
    reason: str


class ConfirmScreen(ModalScreen[Decision | None]):
    """A blocking confirmation for an approval-tier action.

    Shows the exact repository, remote, branch and action in plain text. Nothing happens until
    a name (and a reason, when asked) is typed and Confirm is pressed; Escape cancels.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, target: Target, *, ask_reason: bool = True, title: str = "") -> None:
        super().__init__()
        self.target = target
        self.ask_reason = ask_reason
        self.heading = title or "CONFIRM APPROVAL-TIER ACTION"

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(f"▓▓ {self.heading} ▓▓", id="confirm-title")
            yield Plain(
                "\n".join(
                    [
                        f"Action:     {self.target.action}",
                        f"Repository: {self.target.repository}",
                        f"Remote:     {safe_url(self.target.remote)}",
                        f"Branch:     {self.target.branch}",
                    ]
                ),
                id="confirm-target",
            )
            yield Input(placeholder="Your name (required)", id="confirm-name")
            if self.ask_reason:
                yield Input(placeholder="Reason (required)", id="confirm-reason")
            yield Plain("", id="confirm-error")
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", id="confirm-yes", variant="warning")
                yield Button("Cancel", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.focus_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-no":
            self.dismiss(None)
            return
        name = self.query_one("#confirm-name", Input).value.strip()
        reason = self.query_one("#confirm-reason", Input).value.strip() if self.ask_reason else ""
        if not name or (self.ask_reason and not reason):
            needed = "your name and a reason" if self.ask_reason else "your name"
            self.query_one("#confirm-error", Plain).set(f"Type {needed} to confirm.")
            return
        self.dismiss(Decision(name, reason))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---- palette and help --------------------------------------------------------------------------

COMMANDS = [
    ("mission", "mission control (projects)"),
    ("tasks", "task list of the approved plan"),
    ("checklist <TASK>", "coding checklist of a task"),
    ("run <run-id>", "an agent run"),
    ("patch <patch-id>", "a patch review"),
    ("ci <run-id>", "the CI status of a run"),
    ("theme <name>", "cyber-8 | green-phosphor | amber-terminal | mono"),
    ("help", "keys and commands"),
    ("back", "previous screen"),
    ("quit", "leave the TUI"),
]


class PaletteScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="palette"):
            yield Label("▓▓ COMMAND ▓▓")
            yield Input(placeholder=":command  (e.g. tasks, theme mono, quit)", id="palette-input")
            yield Plain("\n".join(f"{c:<18} {d}" for c, d in COMMANDS))

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP = """\
Keyboard only. Global:  :  command palette   ?  help   Esc  back   q  quit

Mission control   Enter select project   t  tasks
Task list         Enter checklist        g  start task-level run
Checklist         Enter open run         n  run next step   r  resume step   p  patch
Agent run         s  stop   r  resume   p  patch   c  CI status
Patch review      a  accept   x  reject   m  commit   u  push   o  pull request
CI status         a  approve CI branch

Approval-tier actions (accept, CI branch approval, push, pull request) always open a
confirmation showing the exact repository, remote, branch and action.
The model never pushes: pushing is only ever this explicit, confirmed action."""


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help"):
            yield Label("▓▓ HELP ▓▓")
            yield Plain(HELP, id="help-text")

    def action_close(self) -> None:
        self.dismiss(None)


# ---- mission control ---------------------------------------------------------------------------


class MissionScreen(Base):
    TITLE_LABEL = "MISSION CONTROL"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("t", "tasks", "Tasks")]

    def body(self) -> ComposeResult:
        with Horizontal():
            table: DataTable[str] = DataTable(id="projects", cursor_type="row")
            yield _framed(table, "projects")
            yield _framed(Plain("Select a project and press Enter.", id="mission"), "status")

    def on_mount(self) -> None:
        table = self.query_one("#projects", DataTable)
        table.add_columns("NAME", "STATUS")
        rows = self.attempt(self.backend.projects) or []
        for row in rows:
            table.add_row(row.name, row.status, key=str(row.id))
        if not rows:
            self.say("No projects yet. Create one in the web UI (archpilot ui).")
        if self.archpilot.project is not None:
            self.show(self.archpilot.project)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        rows = self.attempt(self.backend.projects) or []
        chosen = next((r for r in rows if str(r.id) == key), None)
        if chosen is not None:
            self.show(chosen)

    def show(self, project: ProjectRow) -> None:
        view = self.attempt(lambda: self.backend.mission(project.id))
        if view is None:
            return
        self.archpilot.project = project
        self.archpilot.plan_id = view.plan_id
        repo = view.repository
        lines = [
            f"Project:      {view.project.name}  ({view.project.id})",
            f"Architecture: {view.architecture}",
            f"Plan:         {view.plan}",
        ]
        if repo is None:
            lines.append("Repository:   not configured")
        else:
            lines += [
                f"Repository:   {repo.clone_folder}  ({'cloned' if repo.cloned else 'not cloned'})",
                f"Remote:       {safe_url(repo.remote_url)}  [{repo.provider}]",
                f"Branch:       {repo.current_branch or '-'}  (base {repo.base_branch}, "
                f"prefix {repo.branch_prefix})",
                f"PAT:          {repo.pat}",
            ]
        self.query_one("#mission", Plain).set("\n".join(lines))
        self.say(f"Project {view.project.name} selected. t = tasks")

    def action_tasks(self) -> None:
        self.archpilot.open_tasks()


# ---- task list ---------------------------------------------------------------------------------


class TasksScreen(Base):
    TITLE_LABEL = "TASKS"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("g", "go", "Run task")]

    def __init__(self, project_id: uuid.UUID, plan_id: uuid.UUID) -> None:
        super().__init__()
        self.project_id = project_id
        self.plan_id = plan_id

    def body(self) -> ComposeResult:
        table: DataTable[str] = DataTable(id="tasks", cursor_type="row")
        yield _framed(table, "approved plan")

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns("KEY", "STATUS", "KIND", "TITLE", "DEPENDS ON")
        for t in self.attempt(lambda: self.backend.tasks(self.project_id, self.plan_id)) or []:
            table.add_row(
                t.key,
                badge(t.status),
                t.kind,
                Text(t.title),
                ", ".join(t.depends_on) or "-",
                key=t.key,
            )
        table.focus()

    def _selected(self) -> str | None:
        table = self.query_one("#tasks", DataTable)
        if table.row_count == 0:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.archpilot.open_checklist(event.row_key.value)

    def action_go(self) -> None:
        key = self._selected()
        if key is None:
            return
        run_id = self.attempt(
            lambda: self.backend.start_task_run(self.project_id, self.plan_id, key)
        )
        if run_id is not None:
            self.archpilot.open_run(run_id)


# ---- coding checklist --------------------------------------------------------------------------


class ChecklistScreen(Base):
    TITLE_LABEL = "CODING CHECKLIST"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "next", "Run next step"),
        Binding("r", "resume", "Resume step"),
        Binding("p", "patch", "Patch"),
    ]

    def __init__(self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str) -> None:
        super().__init__()
        self.project_id = project_id
        self.plan_id = plan_id
        self.task_key = task_key
        self.info: ChecklistInfo | None = None

    def body(self) -> ComposeResult:
        yield Plain("", id="checklist-head")
        steps: DataTable[str] = DataTable(id="steps", cursor_type="row")
        yield _framed(steps, "steps")
        runs: DataTable[str] = DataTable(id="runs", cursor_type="row")
        yield _framed(runs, "runs of this task")

    def on_mount(self) -> None:
        self.query_one("#steps", DataTable).add_columns(
            "STEP", "STATUS", "TITLE", "DEPENDS ON", "RESUMABLE"
        )
        self.query_one("#runs", DataTable).add_columns("RUN", "STATUS", "STEPS", "STARTED")
        self.reload()
        self.query_one("#steps", DataTable).focus()

    def on_screen_resume(self) -> None:
        self.reload()

    def reload(self) -> None:
        info = self.attempt(
            lambda: self.backend.checklist(self.project_id, self.plan_id, self.task_key)
        )
        runs = self.attempt(
            lambda: self.backend.task_runs(self.project_id, self.plan_id, self.task_key)
        )
        self.info = info
        steps = self.query_one("#steps", DataTable)
        steps.clear()
        if info is not None:
            version = f"v{info.version} " if info.version is not None else ""
            self.query_one("#checklist-head", Plain).set(
                f"Task {info.task_key}   checklist {version}{info.status}   "
                f"next step: {info.next_step or '-'}"
            )
            for st in info.steps:
                steps.add_row(
                    st.key,
                    badge(st.status),
                    Text(st.title),
                    ", ".join(st.depends_on) or "-",
                    "yes" if st.resumable else "no",
                    key=st.key,
                )
        table = self.query_one("#runs", DataTable)
        table.clear()
        for r in runs or []:
            table.add_row(
                str(r.id),
                badge(r.status),
                f"{r.steps_taken}/{r.max_steps}",
                r.started_at.strftime("%Y-%m-%d %H:%M"),
                key=str(r.id),
            )

    def _step(self) -> Any:
        table = self.query_one("#steps", DataTable)
        if self.info is None or table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((s for s in self.info.steps if s.key == key), None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "runs" and event.row_key.value:
            self.archpilot.open_run(uuid.UUID(event.row_key.value))
            return
        step = self._step()
        if step is not None and step.run_id is not None:
            self.archpilot.open_run(step.run_id)
        elif step is not None:
            self.say(f"Step {step.key} has not run yet. n = run next step")

    def action_next(self) -> None:
        run_id = self.attempt(
            lambda: self.backend.run_next_step(self.project_id, self.plan_id, self.task_key)
        )
        if run_id is not None:
            self.archpilot.open_run(run_id)

    def action_resume(self) -> None:
        step = self._step()
        if step is None:
            return
        run_id = self.attempt(
            lambda: self.backend.resume_step(self.project_id, self.plan_id, self.task_key, step.key)
        )
        if run_id is not None:
            self.archpilot.open_run(run_id)

    def action_patch(self) -> None:
        step = self._step()
        if step is None or step.patch_id is None:
            self.say("The selected step has no patch.")
            return
        self.archpilot.open_patch(step.patch_id)


# ---- agent run ---------------------------------------------------------------------------------


def render_steps(run: RunResponse) -> str:
    lines: list[str] = []
    for st in run.steps:
        tool = st.tool or "-"
        tier = st.tier or "-"
        model_tier = ""
        if st.call is not None and (st.call.tier or st.call.deployment):
            model_tier = f"  model {st.call.deployment or '-'} ({st.call.tier or '-'})"
        lines.append(
            f"#{st.step_no:<3} {st.action:<18} tool={tool:<18} tier={tier:<8} "
            f"{st.status:<8} {st.path or ''}{model_tier}"
        )
        if st.error_code:
            lines.append(f"       error: {st.error_code}")
        if st.result_summary:
            lines.append(f"       {st.result_summary}")
        for h in st.hooks:
            lines.append(f"       hook {h.hook} @{h.point}: {h.result.upper()}  {h.reason}")
        for f in st.findings:
            lines.append(f"       finding {f.tool} {f.file}:{f.line} {f.code} {f.message}")
        if st.finding_total is not None:
            lines.append(f"       findings: {st.finding_total}")
    return "\n".join(lines) or "Waiting for the first step..."


class RunScreen(Base):
    TITLE_LABEL = "AGENT RUN"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "stop", "Stop"),
        Binding("r", "resume", "Resume"),
        Binding("p", "patch", "Patch"),
        Binding("c", "ci", "CI status"),
    ]

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__()
        self.project_id = project_id
        self.run_id = run_id
        self.view: RunResponse | None = None
        self.updates = 0
        self._timer: Timer | None = None

    def body(self) -> ComposeResult:
        yield _framed(Plain("", id="run-head"), "run")
        yield _framed(Plain("", id="run-budget"), "budget")
        with VerticalScroll(id="run-scroll"):
            yield _framed(Plain("", id="run-steps"), "live steps")

    def on_mount(self) -> None:
        self.refresh_run()
        self._timer = self.set_interval(POLL_SECONDS, self.refresh_run)

    def refresh_run(self) -> None:
        view = self.attempt(lambda: self.backend.run(self.project_id, self.run_id))
        if view is None:
            return
        self.view = view
        self.updates += 1
        self.query_one("#run-head", Plain).set(
            "\n".join(
                [
                    f"Run {view.id}   task {view.task_key}   {badge(view.status)} {view.status}"
                    + (f" ({view.stop_reason})" if view.stop_reason else ""),
                    f"Model {view.model or '-'}   deployment {view.deployment or '-'}   "
                    f"provider {view.provider}",
                    f"Patch {view.patch_id or '-'}",
                    *([f"Summary: {view.summary}"] if view.summary else []),
                ]
            )
        )
        used = view.input_tokens + view.output_tokens
        seconds = view.duration_ms / 1000
        self.query_one("#run-budget", Plain).set(
            "\n".join(
                [
                    f"Tokens {bar(used, view.token_budget)}  {used}/{view.token_budget}",
                    f"Time   {bar(seconds, view.time_limit_seconds)}  "
                    f"{seconds:.0f}s/{view.time_limit_seconds:.0f}s",
                    f"Steps  {bar(view.steps_taken, view.max_steps)}  "
                    f"{view.steps_taken}/{view.max_steps}",
                ]
            )
        )
        self.query_one("#run-steps", Plain).set(render_steps(view))
        if view.status != "running":
            self._stop_polling()

    def _stop_polling(self) -> None:
        timer = self._timer
        if timer is not None:
            timer.stop()
            self._timer = None

    def action_stop(self) -> None:
        if self.attempt(lambda: self.backend.stop_run(self.project_id, self.run_id)) is None:
            return
        self.say("Stop requested; the run stops after its current step.")
        self.refresh_run()

    def action_resume(self) -> None:
        new = self.attempt(lambda: self.backend.resume_run(self.project_id, self.run_id))
        if new is not None:
            self.app.switch_screen(RunScreen(self.project_id, new))

    def action_patch(self) -> None:
        if self.view is None or self.view.patch_id is None:
            self.say("This run has no patch yet.")
            return
        self.archpilot.open_patch(self.view.patch_id)

    def action_ci(self) -> None:
        self.app.push_screen(CIScreen(self.project_id, self.run_id))


# ---- patch review ------------------------------------------------------------------------------


class PatchScreen(Base):
    TITLE_LABEL = "PATCH REVIEW"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "accept", "Accept"),
        Binding("x", "reject", "Reject"),
        Binding("m", "commit", "Commit"),
        Binding("u", "push", "Push"),
        Binding("o", "pull_request", "Pull request"),
    ]

    def __init__(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> None:
        super().__init__()
        self.project_id = project_id
        self.patch_id = patch_id
        self.patch: PatchView | None = None
        self.branch: str | None = None

    def body(self) -> ComposeResult:
        yield _framed(Plain("", id="patch-head"), "patch")
        with Horizontal():
            files: DataTable[str] = DataTable(id="files", cursor_type="row")
            yield _framed(files, "files")
            with VerticalScroll(id="diff-scroll"):
                yield _framed(Plain("", id="diff"), "unified diff")

    def on_mount(self) -> None:
        self.query_one("#files", DataTable).add_columns("CHANGE", "PATH")
        self.reload()
        self.query_one("#files", DataTable).focus()

    def reload(self) -> None:
        patch = self.attempt(lambda: self.backend.patch(self.project_id, self.patch_id))
        if patch is None:
            return
        self.patch = patch
        cov = patch.coverage
        lines = [
            f"Patch {patch.id}   task {patch.task_key} v{patch.version}   "
            f"{badge(patch.status)} {patch.status}",
            f"Coverage: {cov['covered']} covered, {cov['missing']} missing, "
            f"{cov['unchecked']} unchecked",
            *(f"  missing {m}" for m in patch.missing),
        ]
        if patch.removals:
            lines.append("Removes from accepted files (confirm on accept):")
            lines += [f"  {f}: {n}" for f, n in patch.removals]
        if patch.stale:
            lines.append("Stale against the workspace: " + "; ".join(patch.stale))
        if patch.reviewed_by:
            lines.append(f"Reviewed by {patch.reviewed_by}: {patch.review_reason or ''}")
        self.query_one("#patch-head", Plain).set("\n".join(lines))
        table = self.query_one("#files", DataTable)
        table.clear()
        for i, f in enumerate(patch.files):
            table.add_row(f.change, Text(f.path), key=str(i))
        self.show_file(0)

    def show_file(self, index: int) -> None:
        if self.patch is None or not self.patch.files:
            self.query_one("#diff", Plain).set("(no files)")
            return
        f = self.patch.files[min(index, len(self.patch.files) - 1)]
        note = "\n... diff truncated" if f.truncated else ""
        self.query_one("#diff", Plain).set(f"{f.path}\n{f.diff}{note}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self.show_file(int(event.row_key.value))

    def action_accept(self) -> None:
        target = self.attempt(lambda: self.backend.accept_target(self.project_id, self.patch_id))
        if target is None:
            return

        def done(decision: Decision | None) -> None:
            if decision is None:
                self.say("Accept cancelled.")
                return
            accepted = self.attempt(
                lambda: self.backend.accept(
                    self.project_id,
                    self.patch_id,
                    decision.name,
                    decision.reason,
                    removal_reason=decision.reason,
                )
            )
            if accepted is not None:
                self.reload()
                self.say(f"Patch accepted by {decision.name}.")

        self.app.push_screen(ConfirmScreen(target), done)

    def action_reject(self) -> None:
        target = Target(
            f"REJECT patch {self.patch_id}", "(ArchPilotAI workspace)", "(none)", "(none)"
        )

        def done(decision: Decision | None) -> None:
            if decision is None:
                self.say("Reject cancelled.")
                return
            rejected = self.attempt(
                lambda: self.backend.reject(
                    self.project_id, self.patch_id, decision.name, decision.reason
                )
            )
            if rejected is not None:
                self.reload()
                self.say(f"Patch rejected by {decision.name}.")

        self.app.push_screen(ConfirmScreen(target, title="REJECT PATCH"), done)

    def action_commit(self) -> None:
        branch = self.attempt(lambda: self.backend.commit(self.project_id, self.patch_id))
        if branch is not None:
            self.branch = branch
            self.say(f"Committed on local branch {branch} (not pushed). u = push")

    def action_push(self) -> None:
        target = self.attempt(lambda: self.backend.push_target(self.project_id, self.patch_id))
        if target is None:
            return

        def done(decision: Decision | None) -> None:
            if decision is None:
                self.say("Push cancelled.")
                return
            pushed = self.attempt(
                lambda: self.backend.push(self.project_id, target.branch, decision.name)
            )
            if pushed is not None:
                self.say(f"Pushed {pushed}.")

        self.app.push_screen(ConfirmScreen(target, ask_reason=False), done)

    def action_pull_request(self) -> None:
        target = self.attempt(
            lambda: self.backend.pull_request_target(self.project_id, self.patch_id)
        )
        if target is None:
            return

        def done(decision: Decision | None) -> None:
            if decision is None:
                self.say("Pull request cancelled.")
                return
            outcome = self.attempt(
                lambda: self.backend.pull_request(self.project_id, self.patch_id)
            )
            if outcome is not None:
                self.say(outcome)

        self.app.push_screen(ConfirmScreen(target, ask_reason=False), done)


# ---- CI status ---------------------------------------------------------------------------------


def render_ci(ci: dict[str, Any] | None) -> str:
    if ci is None:
        return "This run has no CI repair loop."
    counts = ci.get("counts") or {}
    return "\n".join(
        [
            f"Branch:      {ci.get('branch') or '-'}",
            f"Approval:    {ci.get('approval')}"
            + (f" by {ci['approved_by']}" if ci.get("approved_by") else ""),
            f"Phase:       {ci.get('phase')}",
            f"Iterations:  {bar(ci.get('iteration') or 0, ci.get('max_iterations') or 0)}  "
            f"{ci.get('iteration') or 0}/{ci.get('max_iterations') or 0}",
            f"Workflow:    {ci.get('workflow_status') or '-'}  {ci.get('workflow_url') or ''}",
            f"Commit:      {ci.get('commit') or '-'}",
            f"New: {counts.get('new', 0)}   Fixed: {counts.get('fixed', 0)}   "
            f"Pre-existing: {counts.get('pre_existing', 0)}",
        ]
    )


class CIScreen(Base):
    TITLE_LABEL = "CI STATUS"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("a", "approve", "Approve CI branch")]

    def __init__(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        super().__init__()
        self.project_id = project_id
        self.run_id = run_id

    def body(self) -> ComposeResult:
        yield _framed(Plain("", id="ci"), "ci loop")

    def on_mount(self) -> None:
        self.reload()
        self.set_interval(2.0, self.reload)

    def reload(self) -> None:
        ci = self.attempt(lambda: self.backend.ci(self.project_id, self.run_id))
        self.query_one("#ci", Plain).set(render_ci(ci))

    def action_approve(self) -> None:
        target = self.attempt(lambda: self.backend.ci_target(self.project_id, self.run_id))
        if target is None:
            return

        def done(decision: Decision | None) -> None:
            if decision is None:
                self.say("CI branch approval cancelled.")
                return
            approved = self.attempt(
                lambda: self.backend.ci_approve(self.project_id, self.run_id, decision.name)
            )
            if approved:
                self.reload()
                self.say(f"CI branch approved by {decision.name}.")

        self.app.push_screen(ConfirmScreen(target, ask_reason=False), done)
